"""Drive a real, interactive Claude Code instance (the TUI) inside a jail via tmux.

Not `claude -p`: each player is a long-lived interactive session with its own
context, tools, permissions and hooks. The referee types into the TUI with
`tmux send-keys`, and learns what happened from the hook log the player's own
Claude Code writes (UserPromptSubmit / tool calls / Stop gate / turn_done).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from ..seat import Seat


class Tmux:
    def __init__(self, socket: Path, conf: Path):
        self.socket, self.conf = socket, conf
        conf.write_text("set -g history-limit 50000\nset -g remain-on-exit on\nset -g status off\n"
                        "set -g default-terminal xterm-256color\n")

    async def run(self, *args: str, check: bool = True) -> str:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "-S", str(self.socket), "-f", str(self.conf), *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        if check and proc.returncode:
            raise RuntimeError(f"tmux {args[0]} failed: {err.decode().strip()}")
        return out.decode(errors="replace")


class HookTail:
    """Incrementally parsed view of a seat's hooks.jsonl."""

    def __init__(self, path: Path):
        self.path, self.offset, self.events = path, 0, []

    def poll(self) -> list[dict]:
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                chunk = f.read()
        except FileNotFoundError:
            return []
        if not chunk:
            return []
        last_nl = chunk.rfind(b"\n")
        if last_nl < 0:
            return []
        self.offset += last_nl + 1
        new = []
        for line in chunk[:last_nl].splitlines():
            try:
                new.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        self.events.extend(new)
        return new

    def find(self, pred, since: int = 0):
        self.poll()
        for e in self.events[since:]:
            if pred(e):
                return e
        return None


class ClaudeTUIDriver:
    kind = "claude"

    def __init__(self, seat: Seat, tmux: Tmux, log):
        self.seat, self.tmux, self.log = seat, tmux, log
        self.tail = HookTail(seat.hooks_log)
        self.target = seat.name
        self.restarts = 0

    @property
    def player(self) -> str:
        return self.seat.player

    async def start(self, timeout: float = 90):
        await self.tmux.run("new-session", "-d", "-s", self.target, "-x", "200", "-y", "60",
                            str(self.seat.launch_script))
        e = await self._wait(lambda e: e.get("event") == "SessionStart", 0, timeout)
        if not e:
            raise RuntimeError(f"{self.player}: Claude Code did not start (screen:\n{await self.screen()})")
        await asyncio.sleep(1.5)  # let the TUI finish its first paint before typing
        self.log("seat_started", player=self.player, pid=await self.pid(), session_id=self.seat.session_id)

    async def _wait(self, pred, since, timeout):
        end = time.time() + timeout
        while time.time() < end:
            e = self.tail.find(pred, since)
            if e:
                return e
            await asyncio.sleep(0.4)
        return None

    async def send(self, text: str) -> bool:
        """Type a message into the TUI and confirm Claude Code received it."""
        text = text.replace("\n", " ").rstrip(";")
        self.tail.poll()
        n0 = len(self.tail.events)
        await self.tmux.run("send-keys", "-t", self.target, "-l", text)
        await asyncio.sleep(0.35)
        await self.tmux.run("send-keys", "-t", self.target, "Enter")
        for _ in range(4):
            if await self._wait(lambda e: e.get("event") == "UserPromptSubmit", n0, 8):
                return True
            await self.tmux.run("send-keys", "-t", self.target, "Enter")
        self.log("send_unconfirmed", player=self.player, text=text[:120])
        return False

    async def open_turn(self, spec: dict, prompt: str):
        self.tail.poll()
        self.turn_start_idx = len(self.tail.events)
        await self.send(prompt)

    async def wait_done(self, spec: dict, deadline: float, nudge_text) -> dict:
        turn = spec["turn"]
        nudged = False
        failures = 0
        since = self.turn_start_idx
        seen = since
        opened = time.time()
        nudge_at = deadline - max(45.0, 0.25 * (deadline - opened))
        while True:
            self.tail.poll()
            for e in self.tail.events[seen:]:
                if e.get("event") == "turn_done" and e.get("turn") == turn:
                    return {"status": "done" if e.get("valid") else "invalid", "record": e}
                if e.get("event") == "StopFailure" and "auth" in str(e.get("error", "")).lower():
                    return {"status": "auth_failed", "record": e}
                if e.get("event") == "StopFailure":
                    failures += 1
                    self.log("stop_failure", player=self.player, turn=turn, error=e.get("error"))
                    if failures <= 3:
                        await asyncio.sleep(5 * failures)
                        await self.send(f"The last request failed. Please continue turn {turn:04d}: "
                                        f"read {spec['inbox_path']} and finish it.")
                if e.get("event") == "Notification":
                    self.log("notification", player=self.player, turn=turn, message=e.get("message"))
            seen = len(self.tail.events)
            if not await self.alive():
                self.log("seat_died", player=self.player, turn=turn, screen=(await self.screen())[-2000:])
                if self.restarts >= 2:
                    return {"status": "dead", "record": None}
                await self.restart()
                await self.send(f"You were restarted. Continue turn {turn:04d}: read {spec['inbox_path']} and finish it.")
            now = time.time()
            if not nudged and nudge_at <= now < deadline:
                nudged = True
                await self.send(nudge_text(int(deadline - now)))
            if now >= deadline:
                await self.tmux.run("send-keys", "-t", self.target, "Escape", check=False)
                return {"status": "timeout", "record": None}
            await asyncio.sleep(0.5)

    async def restart(self):
        self.restarts += 1
        await self.tmux.run("respawn-pane", "-k", "-t", self.target,
                            str(self.seat.launch_script.with_suffix(".resume.sh")), check=False)
        self.tail.poll()
        await self._wait(lambda e: e.get("event") == "SessionStart", len(self.tail.events), 90)
        await asyncio.sleep(1.5)

    async def alive(self) -> bool:
        out = await self.tmux.run("display-message", "-p", "-t", self.target, "#{pane_dead}", check=False)
        return out.strip() == "0"

    async def pid(self) -> int | None:
        out = await self.tmux.run("display-message", "-p", "-t", self.target, "#{pane_pid}", check=False)
        return int(out.strip()) if out.strip().isdigit() else None

    async def screen(self, ansi: bool = True, history: int = 80) -> str:
        args = ["capture-pane", "-p", "-J", "-t", self.target, "-S", f"-{history}"]
        if ansi:
            args.insert(1, "-e")
        return await self.tmux.run(*args, check=False)

    async def stop(self):
        try:
            await self.send("/exit")
            await asyncio.sleep(3)
        finally:
            await self.tmux.run("kill-session", "-t", self.target, check=False)
