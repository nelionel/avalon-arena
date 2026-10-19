"""Stand-in models for arena dry runs and tests: legal random moves, no keys, no cost.

One local HTTP server answers the Anthropic Messages API (/v1/messages, plain or
streamed), OpenAI chat completions (/v1/chat/completions) and the streamed OpenAI
Responses API (/v1/responses). mock_clis/codex and
mock_clis/claude stand in for the Codex app-server and the Claude Code CLI.
Moves come from the turn facts in the referee's message, so any backend
adapter can be exercised end to end. Results say nothing about model skill.

  python3 -m avalon.arena_mock --port 18190
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FACTS = re.compile(r"Turn facts:\n```json\n(.*?)\n```", re.S)
MEMORY = re.compile(r"<memory>\n(.*?)\n</memory>", re.S)
REQUESTS: list = []     # (path, headers, body) of every request, newest last: lets tests inspect what clients send
SCRIPTED: list = []     # (status, body, headers) answered before normal replies: lets tests inject provider errors


def mock_reply(prompt: str) -> str:
    """A legal move for the turn described in `prompt` (deterministic per prompt)."""
    found = FACTS.findall(prompt or "")
    if not found:
        return '{"ok": true}' if '{"ok": true}' in (prompt or "") else "I will follow the referee's instructions."
    f = json.loads(found[-1])
    rng = random.Random(hashlib.sha256(prompt.encode()).hexdigest())
    k, me = f["kind"], f["you"]
    others = [p for p in f["players"] if p != me]
    move: dict = {"reason": f"Mock model: a random legal {k} move."}
    if k == "onboard":
        move["ack"] = True
    elif k == "propose":
        move.update(team=[me] + rng.sample(others, f["team_size"] - 1), message="Mock pitch: trust this team.")
    elif k == "discuss":
        move.update(message=rng.choice([None, "Mock: I have doubts.", "Mock: looks fine to me."]), ready_to_vote=True)
    elif k == "vote":
        move["vote"] = rng.choice(["approve", "approve", "reject"])
    elif k == "quest":
        move["card"] = rng.choice(f["options"])
    elif k == "evil_huddle":
        move.update(message="Mock huddle thought.", suggested_target=rng.choice(f["candidates"]))
    elif k == "assassinate":
        move.update(target=rng.choice(f["candidates"]), message="Mock: I name you.")
    else:
        move["message"] = "Mock: good game."
    if k not in ("onboard", "reflect"):
        move["beliefs"] = {p: round(rng.random(), 2) for p in others}
    if f.get("memory_words"):           # tournament players rewrite their memory every turn: keep the last lines
        prev = MEMORY.search(prompt)
        lines = (prev.group(1).splitlines() if prev else [])[-11:]
        move["memory"] = "\n".join(lines + [f"- turn {f['turn']} ({k}): mock note {rng.randrange(1000)}"])
    return "Thinking it over.\n```json\n" + json.dumps(move) + "\n```"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, status, out, headers=None):
        data = json.dumps(out).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        REQUESTS.append((self.path, dict(self.headers), body))
        del REQUESTS[:-50]
        if SCRIPTED:
            return self.send_json(*SCRIPTED.pop(0))
        path = self.path.split("?")[0].rstrip("/")
        if path.endswith("/responses"):                 # OpenAI Responses API, as Codex uses it
            return self.stream_responses(body)
        users = [m for m in body.get("messages", []) if m.get("role") == "user"] or [{}]
        # (content may be a string or OpenAI/Anthropic-style text blocks, e.g. with cache_control)
        last = users[-1].get("content")     # clients such as Claude Code append system messages after it
        if isinstance(last, list):
            last = "".join(b.get("text", "") for b in last if isinstance(b, dict))
        text = mock_reply(last or "")
        if self.path.split("?")[0].rstrip("/").endswith("/v1/messages") and body.get("stream"):
            return self.stream_anthropic(body, text, last)
        if self.path.split("?")[0].rstrip("/").endswith("/v1/messages"):
            out = {"type": "message", "role": "assistant", "model": body.get("model"),
                   "content": [{"type": "thinking", "thinking": "Mock thinking."}, {"type": "text", "text": text}],
                   "usage": {"input_tokens": len(last or "") // 4, "output_tokens": len(text) // 4}}
        elif self.path.split("?")[0].rstrip("/").endswith("/chat/completions"):
            prompt_tokens, completion_tokens = len(last or "") // 4, len(text) // 4
            out = {"provider": "Stand-in", "choices": [{"finish_reason": "stop",
                                "message": {"role": "assistant", "content": text, "reasoning": "Mock reasoning."}}],
                   "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                             "prompt_tokens_details": {"cached_tokens": 0},
                             "completion_tokens_details": {"reasoning_tokens": 3},
                             # OpenRouter-style cost in USD, at $0.10 / $0.50 per million tokens
                             "cost": round(prompt_tokens * 1e-7 + completion_tokens * 5e-7, 8)}}
        else:
            self.send_error(404)
            return
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


    def stream_anthropic(self, body, text, prompt):
        """Server-sent events, as clients such as Claude Code request them."""
        usage = {"input_tokens": len(prompt or "") // 4, "output_tokens": len(text) // 4}
        events = [("message_start", {"type": "message_start", "message": {
                      "id": "msg_mock", "type": "message", "role": "assistant", "model": body.get("model"), "content": [],
                      "stop_reason": None, "stop_sequence": None, "usage": {**usage, "output_tokens": 1}}}),
                  ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                  ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
                  ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                  ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                     "usage": {"output_tokens": usage["output_tokens"]}}),
                  ("message_stop", {"type": "message_stop"})]
        data = "".join(f"event: {name}\ndata: {json.dumps(ev)}\n\n" for name, ev in events).encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


    def stream_responses(self, body):
        items = body.get("input") or []
        users = [i for i in items if isinstance(i, dict) and i.get("role") == "user"]
        prompt = ""
        if users:
            content = users[-1].get("content")
            prompt = content if isinstance(content, str) else "".join(
                c.get("text", "") for c in content or [] if isinstance(c, dict))
        text = mock_reply(prompt)
        usage = {"input_tokens": len(prompt) // 4, "input_tokens_details": {"cached_tokens": 0},
                 "output_tokens": len(text) // 4, "output_tokens_details": {"reasoning_tokens": 0},
                 "total_tokens": (len(prompt) + len(text)) // 4}
        part = {"type": "output_text", "text": text, "annotations": []}
        item = {"id": "msg_mock", "type": "message", "status": "completed", "role": "assistant", "content": [part]}
        resp = {"id": "resp_mock", "object": "response", "status": "in_progress", "model": body.get("model"), "output": []}
        events = [{"type": "response.created", "response": resp},
                  {"type": "response.output_item.added", "output_index": 0,
                   "item": {**item, "status": "in_progress", "content": []}},
                  {"type": "response.content_part.added", "item_id": "msg_mock", "output_index": 0, "content_index": 0,
                   "part": {**part, "text": ""}},
                  {"type": "response.output_text.delta", "item_id": "msg_mock", "output_index": 0, "content_index": 0,
                   "delta": text},
                  {"type": "response.output_text.done", "item_id": "msg_mock", "output_index": 0, "content_index": 0,
                   "text": text},
                  {"type": "response.content_part.done", "item_id": "msg_mock", "output_index": 0, "content_index": 0,
                   "part": part},
                  {"type": "response.output_item.done", "output_index": 0, "item": item},
                  {"type": "response.completed", "response": {**resp, "status": "completed", "output": [item], "usage": usage}}]
        data = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(port: int = 0) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18190)
    serve(ap.parse_args().port).serve_forever()
