"""Model registry (models.toml) and the chat backends model players talk through.

Every backend keeps one conversation per seat: start(system) once, then
send(text) per turn returns a Reply. HTTP calls use only the standard library.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "models.toml"
LOCAL_REGISTRY = ROOT / "models.local.toml"
CREDENTIALS = ROOT / "secrets" / "credentials.env"


class ModelError(RuntimeError):
    """A model could not be configured or did not answer.

    `fatal` marks errors that retrying cannot fix (a bad key, no credits, a rejected request), as opposed to
    a provider that stayed unreachable or overloaded through every retry.
    """

    def __init__(self, message: str, *, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


@dataclass
class ModelSpec:
    key: str
    label: str
    backend: str
    model: str
    maker: str = ""
    color: str = "#888888"
    base_url: str | None = None
    key_env: str | None = None
    max_tokens: int = 8000
    token_param: str = "max_tokens"
    thinking_budget: int = 0
    effort: str | None = None
    concurrency: int = 4
    timeout: float = 280.0
    params: dict = field(default_factory=dict)
    relay_dir: str | None = None        # relay backend: where requests and replies are exchanged
    cache: bool = False                 # openai backend: mark system prompt + latest message for prompt caching
                                        # (needed by Anthropic and Alibaba models behind OpenRouter)


def load_models() -> dict[str, ModelSpec]:
    raw = tomllib.loads(REGISTRY.read_text()).get("models", {}) if REGISTRY.exists() else {}
    if LOCAL_REGISTRY.exists():
        for key, over in tomllib.loads(LOCAL_REGISTRY.read_text()).get("models", {}).items():
            raw[key] = {**raw.get(key, {}), **over}
    return {k: ModelSpec(key=k, **v) for k, v in raw.items()}


def get_model(key: str) -> ModelSpec:
    models = load_models()
    if key not in models:
        raise ModelError(f"unknown model {key!r}; models.toml defines: {', '.join(models) or 'none'}")
    return models[key]


def credential(name: str | None) -> str | None:
    if not name:
        return None
    if os.environ.get(name):
        return os.environ[name]
    try:
        for line in CREDENTIALS.read_text().splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and k.strip() == name:
                return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def preflight(spec: ModelSpec) -> str | None:
    """Why this model cannot play here, or None. Checked before a game creates any files."""
    if spec.backend in ("anthropic",) and not credential(spec.key_env):
        return f"{spec.label} needs {spec.key_env} (environment or secrets/credentials.env)"
    if spec.backend == "openai" and spec.key_env and not credential(spec.key_env):
        return f"{spec.label} needs {spec.key_env} (environment or secrets/credentials.env)"
    if spec.backend == "openai" and spec.base_url and urllib.parse.urlsplit(spec.base_url).hostname in ("127.0.0.1", "localhost"):
        try:                                   # a local server (vLLM, an SSH tunnel) must be up
            urllib.request.urlopen(spec.base_url.rstrip("/") + "/models", timeout=3).close()
        except (urllib.error.URLError, OSError) as e:
            return f"{spec.label} has no server at {spec.base_url} ({getattr(e, 'reason', e)}); start bin/serve-qwen or an SSH tunnel"
    if spec.backend == "codex":
        from .drivers.codex_rpc import codex_binary
        try:
            codex_binary()
        except RuntimeError:
            return f"{spec.label} needs a signed-in Codex CLI on this machine (or AVALON_CODEX_BINARY)"
    if spec.backend == "claude-cli" and not claude_binary():
        return f"{spec.label} needs a signed-in Claude Code CLI on this machine (or AVALON_CLAUDE_BINARY)"
    if spec.backend == "relay":
        return None                         # answered by an orchestrator, e.g. Claude Code subagents
    if spec.backend not in BACKENDS:
        return f"{spec.label}: unknown backend {spec.backend!r}"
    return None


def claude_binary() -> str | None:
    return os.environ.get("AVALON_CLAUDE_BINARY") or shutil.which("claude")


@dataclass
class Reply:
    text: str
    reasoning: str = ""
    usage: dict = field(default_factory=dict)
    seconds: float = 0.0
    finish: str | None = None           # "length" when the reply hit the token limit


_SLOTS: dict = {}


def _slot(spec: ModelSpec) -> asyncio.Semaphore:
    key = (id(asyncio.get_running_loop()), spec.key)
    if key not in _SLOTS:
        _SLOTS[key] = asyncio.Semaphore(spec.concurrency)
    return _SLOTS[key]


TRANSIENT = (408, 409, 429, 500, 502, 503, 504, 529)


def _post(url: str, headers: dict, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _retry_after(headers) -> float | None:
    try:
        value = float((headers or {}).get("Retry-After") or "")
        return value if value >= 0 else None
    except (TypeError, ValueError):
        return None


class HTTPChat:
    """Shared retry loop for HTTP chat APIs.

    Transient failures (rate limits, overloads, gateway errors, and the errors OpenRouter reports
    inside an HTTP 200 body when a model fails mid-generation) back off 5, 20, then 60 seconds,
    or longer when the server sends Retry-After. Anything else, such as invalid credentials or no
    credits, stops at once with the server's message.
    """
    DELAYS = (5, 20, 60)

    def __init__(self, spec: ModelSpec, base_url: str | None = None):
        self.spec = spec
        self.base_url = (base_url or spec.base_url or self.default_url).rstrip("/")
        self.messages: list[dict] = []
        self.system = ""
        self.session = None

    async def start(self, system: str, *, workdir: Path | None = None, **_):
        self.system = system
        if workdir is not None:                          # games/<id>/players/pN/work -> one id per seat
            seat = Path(workdir).parent
            self.session = f"avalon-{seat.parent.parent.name}-{seat.name}"[:256]

    def body_error(self, data: dict):
        """(code, message) for an error carried in a successful HTTP response, else None."""
        return None

    async def send(self, text: str) -> Reply:
        self.messages.append({"role": "user", "content": text})
        url, headers, body = self.request()
        started = time.monotonic()
        for attempt, delay in enumerate((*self.DELAYS, None)):
            try:
                async with _slot(self.spec):
                    data = await asyncio.to_thread(_post, url, headers, body, self.spec.timeout)
                err = self.body_error(data)
                if err is None:
                    break
                code, message = err
                if (code is not None and code not in TRANSIENT) or delay is None:
                    self.messages.pop()
                    raise ModelError(f"{self.spec.label}: provider error {code} from {url}: {message}")
                wait = delay
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:600]
                after = _retry_after(e.headers)
                retryable = e.code in TRANSIENT or (e.code == 402 and after is not None)   # 402 + Retry-After: in-flight budget
                if not retryable or delay is None:
                    self.messages.pop()
                    hint = {401: " (check the API key)", 402: " (the account or key is out of credits)",
                            403: " (moderation, guardrail or budget limit)"}.get(e.code, "")
                    raise ModelError(f"{self.spec.label}: HTTP {e.code}{hint} from {url}: {detail}",
                                     fatal=not retryable) from e
                wait = max(delay, after or 0)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if delay is None:
                    self.messages.pop()
                    raise ModelError(f"{self.spec.label}: cannot reach {url}: {e}") from e
                wait = delay
            await asyncio.sleep(wait)
        reply = self.parse(data)
        reply.seconds = round(time.monotonic() - started, 2)
        self.messages.append({"role": "assistant", "content": reply.text or "(empty reply)"})
        return reply

    async def stop(self):
        pass


class AnthropicChat(HTTPChat):
    default_url = "https://api.anthropic.com"

    def request(self):
        s = self.spec
        msgs = [dict(m) for m in self.messages]
        msgs[-1] = {"role": "user", "content": [{"type": "text", "text": msgs[-1]["content"],
                                                 "cache_control": {"type": "ephemeral"}}]}
        body = {"model": s.model, "max_tokens": s.max_tokens, "messages": msgs,
                "system": [{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}], **s.params}
        if s.thinking_budget:
            body["thinking"] = {"type": "enabled", "budget_tokens": s.thinking_budget}
        headers = {"x-api-key": credential(s.key_env) or "", "anthropic-version": "2023-06-01"}
        return f"{self.base_url}/v1/messages", headers, body

    def parse(self, data):
        blocks = data.get("content") or []
        u = data.get("usage") or {}
        return Reply(text="".join(b.get("text", "") for b in blocks if b.get("type") == "text"),
                     finish="length" if data.get("stop_reason") == "max_tokens" else data.get("stop_reason"),
                     reasoning="".join(b.get("thinking", "") for b in blocks if b.get("type") == "thinking"),
                     usage={"input": (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
                            + (u.get("cache_creation_input_tokens") or 0),
                            "cached": u.get("cache_read_input_tokens") or 0, "output": u.get("output_tokens") or 0})


class OpenAIChat(HTTPChat):
    """OpenAI-compatible chat completions: OpenAI, vLLM, OpenRouter, ...

    For OpenRouter it also sends a per-seat session_id (sticky routing keeps a seat on one provider,
    so its prompt cache stays warm) and records the cost OpenRouter reports for every request.
    """
    default_url = "https://api.openai.com/v1"

    @property
    def openrouter(self) -> bool:
        return (urllib.parse.urlsplit(self.base_url).hostname or "").endswith("openrouter.ai")

    def request(self):
        s = self.spec
        msgs = [{"role": "system", "content": self.system}, *self.messages]
        if s.cache:                     # explicit breakpoints: the stable system prompt and the growing history
            msgs = [{**m, "content": [{"type": "text", "text": m["content"], "cache_control": {"type": "ephemeral"}}]}
                    if i in (0, len(msgs) - 1) else m for i, m in enumerate(msgs)]
        body = {"model": s.model, "messages": msgs, s.token_param: s.max_tokens, **s.params}
        if self.openrouter and self.session:
            body["session_id"] = self.session
        key = credential(s.key_env)
        return f"{self.base_url}/chat/completions", ({"authorization": f"Bearer {key}"} if key else {}), body

    def body_error(self, data):
        if isinstance(data.get("error"), dict):
            return data["error"].get("code"), data["error"].get("message")
        choice = (data.get("choices") or [{}])[0]
        if isinstance(choice.get("error"), dict):
            return choice["error"].get("code"), choice["error"].get("message")
        if choice.get("finish_reason") == "error":
            return None, "the provider reported an error while generating"
        return None

    def parse(self, data):
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        u = data.get("usage") or {}
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        if not reasoning and isinstance(msg.get("reasoning_details"), list):
            reasoning = "\n".join(d.get("text") or d.get("summary") or "" for d in msg["reasoning_details"]
                                  if isinstance(d, dict))
        usage = {"input": u.get("prompt_tokens") or 0, "output": u.get("completion_tokens") or 0,
                 "cached": (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0}
        thought = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
        if thought:
            usage["reasoning"] = thought
        if u.get("cost") is not None:
            usage["cost"] = float(u["cost"])
        if data.get("provider"):                 # OpenRouter: who served this move (e.g. "Claude Platform on AWS")
            usage["provider"] = data["provider"]
        return Reply(text=msg.get("content") or "", reasoning=reasoning, usage=usage, finish=choice.get("finish_reason"))


class CodexChat:
    """GPT models through the signed-in Codex app-server: one persistent thread per seat, no network or writes."""

    def __init__(self, spec: ModelSpec, base_url=None):
        self.spec, self.rpc, self.thread_id = spec, None, None
        self.done = asyncio.Event()
        self.items: list[dict] = []
        self.result: dict = {}
        self.usage: dict = {}

    def _notify(self, msg):
        method, p = msg.get("method", ""), msg.get("params", {})
        if method == "item/completed":
            self.items.append(p.get("item", {}))
        elif method == "thread/tokenUsage/updated":
            last = (p.get("tokenUsage") or {}).get("last") or {}
            self.usage = {"input": last.get("inputTokens") or 0, "cached": last.get("cachedInputTokens") or 0,
                          "output": (last.get("outputTokens") or 0) + (last.get("reasoningOutputTokens") or 0)}
        elif method == "turn/completed":
            self.result = p.get("turn", {})
            self.done.set()
        elif method == "avalon/serverClosed":
            self.result = {"status": "failed", "error": {"message": "Codex app-server closed"}}
            self.done.set()

    async def start(self, system: str, *, workdir: Path, log_path: Path):
        from .drivers.codex_rpc import CodexRPC
        scratch = Path(workdir) / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        self.rpc = CodexRPC(scratch, [scratch], [scratch], log_path, self._notify)
        await self.rpc.start()
        models = await self.rpc.call("model/list", {"limit": 100, "includeHidden": True})
        if not any(m.get("model") == self.spec.model for m in models.get("data", [])):
            raise ModelError(f"{self.spec.label}: Codex does not offer {self.spec.model} to this login; no fallback")
        t = await self.rpc.call("thread/start", {
            "model": self.spec.model, "cwd": str(scratch), "approvalPolicy": "never", "permissions": self.rpc.profile,
            "baseInstructions": system, "allowProviderModelFallback": False, "ephemeral": False, "serviceName": "avalon"})
        if t.get("model") != self.spec.model:
            raise ModelError(f"{self.spec.label}: Codex started {t.get('model')} instead of {self.spec.model}")
        self.thread_id = t["thread"]["id"]

    async def send(self, text: str) -> Reply:
        started = time.monotonic()
        async with _slot(self.spec):
            self.done.clear()
            self.items, self.result, self.usage = [], {}, {}
            await self.rpc.call("turn/start", {
                "threadId": self.thread_id, "model": self.spec.model, "effort": self.spec.effort or "medium",
                "permissions": self.rpc.profile, "approvalPolicy": "never", "input": [{"type": "text", "text": text}]})
            try:
                await asyncio.wait_for(self.done.wait(), self.spec.timeout)
            except asyncio.TimeoutError as e:
                raise ModelError(f"{self.spec.label}: no answer within {self.spec.timeout:.0f}s") from e
        if self.result.get("status") != "completed":
            raise ModelError(f"{self.spec.label}: Codex turn {self.result.get('status')}: {self.result.get('error')}")
        texts = [i.get("text") or "" for i in self.items if i.get("type") == "agentMessage"]
        def text_of(x):
            return x.get("text", "") if isinstance(x, dict) else str(x)
        thoughts = [" ".join(text_of(x) for x in i.get("summary")) if isinstance(i.get("summary"), list)
                    else text_of(i.get("summary") or i.get("text") or "") for i in self.items if i.get("type") == "reasoning"]
        return Reply(text=texts[-1] if texts else "", reasoning="\n".join(t for t in thoughts if t),
                     usage=self.usage, seconds=round(time.monotonic() - started, 2))

    async def stop(self):
        if self.rpc:
            await self.rpc.close()


class ClaudeCLIChat:
    """Claude models through a signed-in Claude Code CLI in print mode: no tools, one resumed session per seat."""

    def __init__(self, spec: ModelSpec, base_url=None):
        self.spec, self.session, self.system, self.cwd, self.turns = spec, str(uuid.uuid4()), "", None, 0

    async def start(self, system: str, *, workdir: Path, log_path: Path):
        self.system, self.cwd = system, Path(workdir) / "scratch"
        self.cwd.mkdir(parents=True, exist_ok=True)

    async def send(self, text: str) -> Reply:
        argv = [claude_binary() or "claude", "-p", "--model", self.spec.model, "--output-format", "json",
                "--tools", "", "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands"]
        argv += ["--resume", self.session] if self.turns else ["--session-id", self.session, "--system-prompt", self.system]
        if self.spec.effort:
            argv += ["--effort", self.spec.effort]
        started = time.monotonic()
        async with _slot(self.spec):
            proc = await asyncio.create_subprocess_exec(*argv, cwd=self.cwd, stdin=asyncio.subprocess.PIPE,
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                out, err = await asyncio.wait_for(proc.communicate(text.encode()), self.spec.timeout)
            except asyncio.TimeoutError as e:
                proc.kill()
                raise ModelError(f"{self.spec.label}: no answer within {self.spec.timeout:.0f}s") from e
        try:
            data = json.loads(out)
        except json.JSONDecodeError as e:
            raise ModelError(f"{self.spec.label}: claude exited {proc.returncode}: {(err or out).decode()[-600:]}") from e
        if data.get("is_error"):
            raise ModelError(f"{self.spec.label}: {data.get('result') or data}")
        self.turns += 1
        u = data.get("usage") or {}
        return Reply(text=data.get("result") or "", seconds=round(time.monotonic() - started, 2),
                     usage={"input": (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
                            + (u.get("cache_creation_input_tokens") or 0),
                            "cached": u.get("cache_read_input_tokens") or 0, "output": u.get("output_tokens") or 0})

    async def stop(self):
        pass


async def ping(spec: ModelSpec, base_url: str | None = None) -> tuple[bool, str]:
    """One tiny real request through the model's backend: (answered with JSON, detail)."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="avalon-ping-") as tmp:
        chat = make_chat(spec, base_url)
        try:
            await chat.start("You are checking that you can answer the Avalon arena referee.",
                             workdir=Path(tmp), log_path=Path(tmp) / "codex.log")
            reply = await asyncio.wait_for(chat.send('Reply with exactly this JSON object and nothing else: {"ok": true}'),
                                           spec.timeout)
        except Exception as e:
            return False, str(e)[:300]
        finally:
            try:
                await chat.stop()
            except Exception:
                pass
    ok = '"ok"' in (reply.text or "")
    return ok, f"{reply.seconds:.1f}s, {'answered' if ok else 'unexpected reply: ' + repr((reply.text or '')[:120])}"


def relay_dir(spec: ModelSpec) -> Path:
    return Path(spec.relay_dir) if spec.relay_dir else ROOT / "games" / ".relay"


class RelayChat:
    """A model that answers outside this process, through files.

    Every turn becomes one self-contained request file (rules, secret role, the seat's whole
    conversation so far, this turn). An orchestrator, such as a Claude Code session that hands
    each request to a subagent, writes the reply file. No API key is involved.
    """

    def __init__(self, spec: ModelSpec, base_url=None):
        self.spec, self.messages, self.system, self.n = spec, [], "", 0

    async def start(self, system: str, *, workdir: Path, log_path: Path):
        self.system = system
        self.dir = relay_dir(self.spec)
        self.dir.mkdir(parents=True, exist_ok=True)
        seat_dir = Path(workdir).parent                       # games/<id>/players/pN
        self.prefix = f"{seat_dir.parent.parent.name}-{seat_dir.name}"

    def render(self, reply_path: Path) -> str:
        earlier = []
        for i in range(0, len(self.messages) - 1, 2):
            earlier.append(f"### Referee\n\n{self.messages[i]['content']}\n\n### You replied\n\n{self.messages[i + 1]['content']}")
        return (f"# Avalon: your private turn ({self.prefix})\n\n"
                "You are one player in a game of The Resistance: Avalon. This file has everything you may use: the "
                "rules, your secret role, your conversation with the referee so far, and THIS TURN at the end. Do "
                "not open, list or search any other file, and do not run commands.\n\n"
                f"Write your reply to: {reply_path}\n"
                "Think as much as you like, then end the reply with exactly one JSON object as THIS TURN asks.\n\n"
                f"## Your instructions, rules and secret role\n\n{self.system}\n\n"
                f"## Earlier turns, oldest first\n\n{chr(10).join(earlier) or '(none yet)'}\n\n"
                f"## THIS TURN\n\n{self.messages[-1]['content']}\n")

    async def send(self, text: str) -> Reply:
        self.messages.append({"role": "user", "content": text})
        self.n += 1
        name = f"{self.prefix}-{self.n:03d}"
        request, reply = self.dir / f"{name}.request.md", self.dir / f"{name}.reply.txt"
        tmp = request.with_suffix(".tmp")
        tmp.write_text(self.render(reply))
        tmp.replace(request)
        started, last_size = time.monotonic(), -1
        while True:
            if reply.exists():
                size = reply.stat().st_size
                if size and size == last_size:                # complete: unchanged for one poll
                    break
                last_size = size
            if time.monotonic() - started > self.spec.timeout:
                self.messages.pop()
                raise ModelError(f"{self.spec.label}: no reply in {reply} within {self.spec.timeout:.0f}s")
            await asyncio.sleep(1.0)
        text_out = reply.read_text(errors="replace")
        request.rename(request.with_suffix(".answered.md"))
        self.messages.append({"role": "assistant", "content": text_out})
        return Reply(text=text_out, seconds=round(time.monotonic() - started, 2))

    async def stop(self):
        pass


def audit_relay(game_id: str, transcripts: Path) -> dict:
    """Check Claude Code subagent transcripts: every file a subagent touched must be one request or reply of one seat."""
    import re
    pat = re.compile(re.escape(f"{game_id}-") + r"(p\d+)-(\d+)\.(request\.md|reply\.txt)$")
    report = {"subagents": 0, "models": {}, "other_tool_calls": [], "crossed_seats": []}
    for f in sorted(Path(transcripts).glob("*.jsonl")):
        touched, mine = set(), False
        calls, models = [], {}
        for line in f.read_text(errors="replace").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            m = r.get("message") or {}
            if r.get("type") == "assistant" and m.get("model"):
                models[m["model"]] = models.get(m["model"], 0) + 1
            for c in m.get("content") if isinstance(m.get("content"), list) else []:
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") != "SubagentHandback":
                    inp = c.get("input") or {}
                    path = str(inp.get("file_path") or inp.get("path") or inp.get("command") or inp)
                    hit = pat.search(path) if c["name"] in ("Read", "Write") else None
                    mine = mine or bool(hit)
                    if hit:
                        touched.add(f"{hit.group(1)}-{hit.group(2)}")
                    else:
                        calls.append({"subagent": f.name, "tool": c["name"], "input": path[:200]})
        if mine:                                   # only subagents that played this game
            report["subagents"] += 1
            report["other_tool_calls"] += calls
            for k, v in models.items():
                report["models"][k] = report["models"].get(k, 0) + v
            if len(touched) > 1:
                report["crossed_seats"].append({"subagent": f.name, "files": sorted(touched)})
    report["clean"] = not report["crossed_seats"] and all(
        c["tool"] == "mcp__ccd_session__read_widget_context" for c in report["other_tool_calls"])
    return report


def pending_relay(spec_dir: Path | None = None) -> list[Path]:
    d = spec_dir or ROOT / "games" / ".relay"
    return sorted(p for p in d.glob("*.request.md") if not p.with_name(p.name.replace(".request.md", ".reply.txt")).exists())


BACKENDS = {"anthropic": AnthropicChat, "openai": OpenAIChat, "codex": CodexChat, "claude-cli": ClaudeCLIChat, "relay": RelayChat}


def make_chat(spec: ModelSpec, base_url: str | None = None):
    return BACKENDS[spec.backend](spec, base_url)
