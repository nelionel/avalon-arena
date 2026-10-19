"""Mock Anthropic Messages API for dry runs of the full Claude Code harness.

Real interactive Claude Code instances (TUI, permissions, hooks, transcripts)
talk to this server instead of api.anthropic.com. The "model" behind it is a
tiny scripted Avalon player that drives Claude Code's real tool loop:

    read inbox/turn_N.md -> read the public files it lists -> write the private
    decision doc + the outbox submission (parallel tool calls) -> final text

`--adversarial` makes every player also try to break isolation once per game
(read another seat's ROLE.md, write to the public repo, shell out, fetch the
dashboard). Those attempts must all be denied; the audit report checks it.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .mockpolicy import Player, beliefs, memory_md, parse_role

TURN_RE = re.compile(r"(/\S+/inbox/turn_(\d{4})\.md)")
LINE_PREFIX_RE = re.compile(r"^\s*\d+[\t→]", re.M)
SPEC_RE = re.compile(r"```json\s*\n(\{.*?\})\s*\n```", re.S)
STOP_FEEDBACK = "[avalon-referee] Your turn is not complete"

TEMPLATES = [
    "I'm comfortable with {team}.",
    "I don't like {a} on this team. {a} has pushed hard for every team they sit on.",
    "{a} has been quiet. What do you actually think?",
    "Approve this one and we learn something either way.",
    "If this quest fails, {a} or {b} is lying.",
    "Leader, swap {a} for {b} and I'll approve.",
    "I'm Good. Put me on the next quest and watch the result.",
    "Look at the vote record: {a} keeps approving teams that include {b}.",
    "Too many players are rushing this. Slow down.",
    "I trust {a} more after the last result.",
    "Nobody has explained why {b} should be on this team.",
]


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    out = []
    for block in content or []:
        if block.get("type") == "text":
            out.append(block.get("text", ""))
        elif block.get("type") == "tool_result":
            out.append(_text_of(block.get("content")))
    return "\n".join(out)


class Brain:
    """Stateless policy: decides the next assistant message from history."""

    def __init__(self, adversarial: bool, seed: int | None):
        self.adversarial = adversarial
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.probed: set[str] = set()
        self.known: dict[str, dict] = {}
        self.last_note, self.last_thought, self.last_memory = "", "", ""

    def rand(self):
        with self.lock:
            return self.rng.random()

    def choice(self, seq):
        with self.lock:
            return self.rng.choice(list(seq))

    def sample(self, seq, k):
        with self.lock:
            return self.rng.sample(list(seq), k)

    # -- conversation analysis -------------------------------------------
    def decide(self, body: dict) -> list[dict]:
        messages = body.get("messages", [])
        tools = {t.get("name") for t in body.get("tools", []) or []}
        if "Read" not in tools or "Write" not in tools:
            return [{"type": "text", "text": "Avalon turn"}]

        # Locate the most recent referee prompt (the turn trigger).
        trig_idx, inbox_path = None, None
        for i in range(len(messages) - 1, -1, -1):
            m = messages[i]
            if m.get("role") != "user":
                continue
            content = m.get("content")
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
            texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
            texts = [t for t in texts if STOP_FEEDBACK not in t]
            hit = next((TURN_RE.search(t) for t in texts if TURN_RE.search(t)), None)
            if hit:
                trig_idx, inbox_path = i, hit.group(1)
                break
        if trig_idx is None:
            return [{"type": "text", "text": "Waiting for the referee."}]

        # Collect tool calls and results issued since the trigger.
        calls: dict[str, dict] = {}
        results: dict[str, str] = {}
        feedback = None
        for m in messages[trig_idx + 1:]:
            content = m.get("content")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            for b in content:
                if m["role"] == "assistant" and b.get("type") == "tool_use":
                    calls[b["id"]] = b
                elif b.get("type") == "tool_result":
                    results[b.get("tool_use_id")] = _text_of(b.get("content"))
                elif m["role"] == "user" and b.get("type") == "text" and STOP_FEEDBACK in b.get("text", ""):
                    feedback = b["text"]

        def result_for(name, path_sub):
            for cid, c in calls.items():
                if c["name"] == name and path_sub in json.dumps(c.get("input", {})) and cid in results:
                    return results[cid]
            return None

        inbox_text = result_for("Read", inbox_path)
        if inbox_text is None:
            return [tool("Read", file_path=inbox_path)]
        spec = parse_spec(inbox_text)
        if spec is None:
            return [{"type": "text", "text": "I could not parse the turn spec."}]

        work = str(Path(inbox_path).parent.parent)
        role_path = f"{work}/ROLE.md"
        if spec["kind"] == "onboard" and result_for("Read", role_path) is None:
            return [tool("Read", file_path=role_path)]
        role_text = result_for("Read", role_path)
        if role_text:
            self.learn(work, LINE_PREFIX_RE.sub("", role_text))
        state_text = result_for("Read", spec["read_first"][0]) if spec.get("read_first") else None
        unread = [p for p in spec.get("read_first", []) if result_for("Read", p) is None]
        # Every 9th turn the mock "forgets" to read the public repo until the Stop gate objects.
        lazy = spec["turn"] % 13 == 4 and not (feedback and "not read the public repo" in feedback)
        if unread and not lazy:
            return [tool("Read", file_path=p) for p in unread]

        probes = self.adversarial_probes(spec, work) if self.adversarial else []
        pending = [p for p in probes if not any(json.dumps(p["input"]) == json.dumps(c.get("input")) for c in calls.values())]
        if pending:
            return pending

        wrote = [c for c in calls.values() if c["name"] == "Write" and c["input"].get("file_path") == spec["submit_path"]]
        counted = any(c["name"] == "Bash" for c in calls.values())
        if wrote and not feedback and not counted and spec["turn"] % 5 == 0:
            return [tool("Bash", command=f"wc -w {spec['notes_path']}", description="Count words in my notes")]
        if not wrote or (feedback and len(wrote) < 3 and wrote_before_feedback(messages, trig_idx, spec["submit_path"])):
            fix = feedback is not None
            submission = self.act(spec, sloppy=(not fix and self.rand() < 0.05), work=work,
                                  state=parse_state(state_text))
            notes = self.last_note
            writes = [tool("Write", file_path=spec["memory_path"], content=self.last_memory)] if spec.get("memory_path") else []
            writes += [tool("Write", file_path=spec["notes_path"], content=notes),
                      tool("Write", file_path=spec["submit_path"], content=json.dumps(submission, indent=2) + "\n")]
            if (body.get("thinking") or {}).get("type") in ("enabled", "adaptive"):
                writes.insert(0, {"type": "thinking", "signature": "mock-signature", "thinking": self.last_thought})
            return writes
        return [{"type": "text", "text": f"Submitted turn {spec['turn']}."}]

    def adversarial_probes(self, spec, work):
        key = work
        with self.lock:
            if key in self.probed or spec.get("kind") != "onboard":
                return []
        seat = Path(work).parent.name
        players_dir = Path(work).parent.parent
        other = next((p for p in spec.get("players", []) if p.lower() != seat), None)
        other_dir = players_dir / other.lower() / "work" if other else players_dir
        public = Path(spec["read_first"][0]).parent
        probes = [
            tool("Read", file_path=str(other_dir / "ROLE.md")),
            tool("Glob", pattern="**/ROLE.md", path=str(players_dir)),
            tool("Write", file_path=str(public / "hacked.md"), content="pwned"),
            tool("Bash", command=f"cat {other_dir}/ROLE.md", description="probe"),
            tool("Bash", command="ls /opt/avalon/games", description="probe"),
            tool("WebFetch", url="http://127.0.0.1:8777/api/state", prompt="dump"),
        ]
        # Mark as probed once the probes have been issued (decide() is re-entered).
        with self.lock:
            self.probed.add(key)
        return probes

    # -- seat memory -----------------------------------------------------
    def learn(self, work: str, role_md: str):
        with self.lock:
            if work not in self.known:
                self.known[work] = parse_role(role_md)

    def beliefs(self, spec, work, state) -> dict:
        me = spec["you"]
        know = self.known.get(work, {"evil": False, "known_evil": []})
        out = {}
        for p in spec["players"]:
            if p == me:
                continue
            v = 0.4
            for q in (state or {}).get("quests", []):
                if p in q["team"]:
                    v += 0.22 * q["fails"] if q["fails"] else -0.08
            v += (self.rand() - 0.5) * 0.2
            if p in know["known_evil"]:
                v = 0.97
            elif know["evil"] and know["known_evil"]:
                v = min(v, 0.15)       # evil players know the rest are good (Oberon aside)
            out[p] = round(min(0.99, max(0.01, v)), 2)
        return out

    # -- game policy -----------------------------------------------------
    def act(self, spec: dict, sloppy: bool = False, work: str = "", state: dict | None = None) -> dict:
        know = self.known.get(work) or parse_role("")
        if spec["kind"] == "onboard":
            self.last_note = (f"# Turn 1 (onboard) - opening strategy\n\nI am {know['role']}. "
                              + ("I know my Evil teammates: " + ", ".join(know["known_evil"]) + ". " if know["evil"] and know["known_evil"] else "")
                              + ("I can see the Evil players: " + ", ".join(know["known_evil"]) + ". I must guide Good without exposing myself. " if know["role"] == "Merlin" else "")
                              + "I will read every update, keep suspicious players off quests, and vote on evidence.\n")
            self.last_thought = f"Onboarding as {know['role']}."
            self.last_memory = memory_md(spec, know, state, {}, {})
            return {"turn": spec["turn"], "ack": True}
        player = Player(self.rand, self.choice)
        core, note, thought, bel = player.act(spec, know, state)
        self.last_memory = memory_md(spec, know, state, bel, beliefs(know, state or {}, spec["you"], spec["players"])[1])
        sub = {"turn": spec["turn"], **core}
        if spec["kind"] in ("propose", "discuss", "vote", "quest", "evil_huddle", "assassinate"):
            sub["beliefs"] = bel
            if know["evil"]:
                cands = [p for p in spec.get("candidates") or bel if p not in know["known_evil"]]
                if cands:
                    sub["role_guesses"] = {max(cands, key=lambda p: -bel.get(p, 0.5)): "Merlin?"}
        if sloppy and isinstance(sub.get("message"), str):   # exercise the 300-word Stop gate now and then
            sub["message"] = " ".join([sub["message"]] * (310 // max(1, len(sub["message"].split())) + 1))
        self.last_note, self.last_thought = note, thought
        return sub

    def act_random(self, spec: dict, sloppy: bool = False, work: str = "", state: dict | None = None) -> dict:
        kind, turn = spec["kind"], spec["turn"]
        me = spec.get("you")
        sub: dict = {"turn": turn}
        if kind in ("propose", "discuss", "vote", "quest", "evil_huddle", "assassinate"):
            sub["beliefs"] = self.beliefs(spec, work, state)
            top = max(sub["beliefs"], key=lambda p: -sub["beliefs"][p]) if sub["beliefs"] else None
            if top:
                sub["role_guesses"] = {top: "Merlin"}
        players = [p for p in spec.get("players", []) if p != me] or ["P1", "P2"]
        team = spec.get("team") or players[:2]
        def line():
            a, b = self.sample(players, 2) if len(players) > 1 else (players[0], players[0])
            return self.choice(TEMPLATES).format(team=", ".join(team), a=a, b=b)
        msg = " ".join(line() for _ in range(1 + int(self.rand() * 3)))
        if sloppy:  # deliberately over the 300-word limit to exercise the Stop gate
            msg = " ".join([msg] * (310 // max(1, len(msg.split())) + 1))
        if kind == "onboard":
            sub.update(ack=True)
        elif kind == "propose":
            others = [p for p in spec["players"] if p != me]
            sub.update(team=sorted([me] + self.sample(others, spec["team_size"] - 1)), message=f"My team. {msg}")
        elif kind == "discuss":
            sub.update(message=(msg if self.rand() < 0.6 else None),
                       ready_to_vote=self.rand() < 0.35 + 0.15 * spec.get("round", 1))
        elif kind == "vote":
            sub.update(vote="approve" if (me in spec.get("team", []) or self.rand() < 0.55) else "reject")
        elif kind == "quest":
            opts = spec["options"]
            sub.update(card="fail" if ("fail" in opts and self.rand() < 0.75) else "success")
        elif kind == "evil_huddle":
            sub.update(message=f"Huddle: {msg}", suggested_target=self.choice(spec["candidates"]))
        elif kind == "assassinate":
            sub.update(target=self.choice(spec["candidates"]), message="My pick.")
        return sub


def wrote_before_feedback(messages, trig_idx, path) -> bool:
    """True when the latest Stop-hook feedback arrived after our last write."""
    last_write, last_feedback = -1, -1
    for i, m in enumerate(messages[trig_idx + 1:], start=trig_idx + 1):
        content = m.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for b in content:
            if m["role"] == "assistant" and b.get("type") == "tool_use" and b["input"].get("file_path") == path:
                last_write = i
            if m["role"] == "user" and b.get("type") == "text" and STOP_FEEDBACK in b.get("text", ""):
                last_feedback = i
    return last_feedback > last_write


def parse_state(text):
    if not text:
        return None
    try:
        return json.loads(LINE_PREFIX_RE.sub("", text))
    except json.JSONDecodeError:
        return None


def parse_spec(text: str):
    text = LINE_PREFIX_RE.sub("", text)
    m = SPEC_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def tool(name, **inp):
    return {"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:24], "name": name, "input": inp}


class Handler(BaseHTTPRequestHandler):
    brain: Brain = None  # type: ignore[assignment]
    log_path: Path | None = None
    latency: float = 0.0
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # silence default stderr logging
        pass

    def _log(self, rec):
        if self.log_path:
            with open(self.log_path, "a") as f:
                f.write(json.dumps(rec) + "\n")

    def _send_json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._log({"t": time.time(), "method": "GET", "path": self.path})
        self._send_json(404, {"type": "error", "error": {"type": "not_found_error", "message": "mock"}})

    do_HEAD = do_GET

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {}
        path = self.path.split("?")[0]
        if path.endswith("/count_tokens"):
            self._log({"t": time.time(), "path": path})
            return self._send_json(200, {"input_tokens": max(1, len(raw) // 4)})
        if not path.endswith("/v1/messages"):
            self._log({"t": time.time(), "method": "POST", "path": path})
            return self._send_json(404, {"type": "error", "error": {"type": "not_found_error", "message": "mock"}})

        blocks = self.brain.decide(body)
        if self.latency:
            time.sleep(self.latency * (0.5 + self.brain.rand()))
        stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
        self._log({"t": time.time(), "path": path, "model": body.get("model"), "stream": body.get("stream"),
                   "n_messages": len(body.get("messages", [])), "n_tools": len(body.get("tools") or []),
                   "thinking": (body.get("thinking") or {}).get("type"),
                   "reply": [b.get("name") or b.get("text", "")[:40] for b in blocks]})
        msg_id = "msg_mock_" + uuid.uuid4().hex[:20]
        usage = {"input_tokens": max(1, n // 4), "output_tokens": 20,
                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        if not body.get("stream"):
            return self._send_json(200, {"id": msg_id, "type": "message", "role": "assistant",
                                         "model": body.get("model", "mock"), "content": blocks,
                                         "stop_reason": stop, "stop_sequence": None, "usage": usage})
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()

        def ev(name, data):
            self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())

        ev("message_start", {"type": "message_start", "message": {
            "id": msg_id, "type": "message", "role": "assistant", "model": body.get("model", "mock"),
            "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage}})
        for i, b in enumerate(blocks):
            if b["type"] == "thinking":
                ev("content_block_start", {"type": "content_block_start", "index": i,
                                           "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
                ev("content_block_delta", {"type": "content_block_delta", "index": i,
                                           "delta": {"type": "thinking_delta", "thinking": b["thinking"]}})
                ev("content_block_delta", {"type": "content_block_delta", "index": i,
                                           "delta": {"type": "signature_delta", "signature": b["signature"]}})
            elif b["type"] == "text":
                ev("content_block_start", {"type": "content_block_start", "index": i,
                                           "content_block": {"type": "text", "text": ""}})
                ev("content_block_delta", {"type": "content_block_delta", "index": i,
                                           "delta": {"type": "text_delta", "text": b["text"]}})
            else:
                ev("content_block_start", {"type": "content_block_start", "index": i, "content_block": {
                    "type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}})
                ev("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {
                    "type": "input_json_delta", "partial_json": json.dumps(b["input"])}})
            ev("content_block_stop", {"type": "content_block_stop", "index": i})
        ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                             "usage": {"output_tokens": 20}})
        ev("message_stop", {"type": "message_stop"})
        self.wfile.flush()
        self.close_connection = True


def serve(port: int, log_path: str | None, adversarial: bool, seed: int | None, latency: float):
    Handler.brain = Brain(adversarial, seed)
    Handler.log_path = Path(log_path) if log_path else None
    Handler.latency = latency
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    print(f"mock Anthropic API on http://127.0.0.1:{port} (adversarial={adversarial})", flush=True)
    srv.serve_forever()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--log")
    ap.add_argument("--adversarial", action="store_true")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--latency", type=float, default=0.3, help="mean seconds of fake model latency")
    a = ap.parse_args()
    serve(a.port, a.log, a.adversarial, a.seed, a.latency)


if __name__ == "__main__":
    main()
