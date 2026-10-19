"""The referee: runs one Avalon game between sandboxed players.

Per turn (strict barrier):
  1. write each actor's private turn file (inbox/turn_NNNN.md + current.json)
  2. prompt every actor at once
  3. wait until *all* actors have submitted (or timed out)
  4. snapshot every actor's private folder into the referee's audit repos
  5. validate submissions, apply them to the engine
  6. publish the turn's public events to the public repo in one commit
Nobody sees anything from a turn until every submission for it is in.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import random
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import prompts
from .drivers.claude_tui import ClaudeTUIDriver, Tmux
from .drivers.scripted import ScriptedDriver
from .drivers.codex_local import CodexDriver, CodexSeat
from .engine import ASSASSINATE, DISCUSS, HUDDLE, OVER, PROPOSE, QUEST, VOTE, Game
from .publicrepo import PublicRepo
from .persistence import GameLease, write_json, sync_jsonl
from .drivers.codex_rpc import CodexRPC
from .drivers.local_bot import bot_driver, seat_plan
from .drivers.model_player import ModelDriver
from .models import get_model
from .roles import EVIL, PRESETS, ROLES, TEAM_SIZES, fails_required, knowledge, roles_in_play_summary, validate_roles
from .seat import ROOT, LocalSeat, Seat, SeatConfig

sys.path.insert(0, str(ROOT / "hooks"))
import turnspec  # noqa: E402

GAMES = ROOT / "games"
SECRETS = ROOT / "secrets"
SLOTS = ROOT / "runtime" / "slots"


@dataclass
class GameConfig:
    players: int = 7
    roles: list[str] | None = None
    driver: str = "claude"              # claude | codex | scripted | bots
    model: str | None = None
    effort: str | None = None
    max_rounds: int = 10
    huddle_rounds: int = 1
    reflect: bool = True
    turn_timeout: float = 300.0
    onboard_timeout: float = 420.0
    allow_bash: bool = True
    mock: bool = False                  # route the players to the local mock API
    mock_adversarial: bool = False
    mock_latency: float = 0.3
    seed: int | None = None
    game_id: str | None = None
    tag: str | None = None
    first_leader: str | None = None
    max_turns: int | None = None        # stop early (tests)
    hook_guard: bool = True             # False only to test the deeper layers on their own
    voice: bool = True                  # render public messages to speech (Kokoro) as the game goes
    credentials_file: str = str(SECRETS / "credentials.env")
    seats: dict[str, str] | None = None  # bots driver: {"P1": "/abs/path/bot.py[:Class]" | "random" | "scripted"}
    fill: str = "scripted"              # bots driver: who plays the seats not named in `seats`
    assignment: dict[str, str] | None = None  # fixed roles by seat (a tournament schedule deals them)
    team_model: str | None = None       # tournament: the models.toml key every "team:" seat plays with

    def __post_init__(self):
        if self.model is None:
            self.model = {"codex": "gpt-6-luna", "claude": "claude-haiku-5-5"}.get(self.driver)


def now() -> float:
    return round(time.time(), 3)


class SlotLock:
    """One uid block per concurrently running game."""

    def __init__(self):
        SLOTS.mkdir(parents=True, exist_ok=True)
        self.fh = None
        self.slot = None

    def acquire(self, requested: int | None = None) -> int:
        for n in ([requested] if requested is not None else range(10)):
            fh = open(SLOTS / f"slot{n}.lock", "w")
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fh.close()
                continue
            self.fh, self.slot = fh, n
            return n
        raise RuntimeError("all 10 game slots are busy")

    def release(self):
        if self.fh:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()


def load_credentials(cfg: GameConfig) -> dict[str, str]:
    creds: dict[str, str] = {}
    path = Path(cfg.credentials_file)
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                creds[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        if os.environ.get(k) and k not in creds:
            creds[k] = os.environ[k]
    allowed = {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL"}
    creds = {k: v for k, v in creds.items() if k in allowed}
    if not ({"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"} & creds.keys()):
        raise SystemExit(
            f"No credentials for the players. Put ANTHROPIC_API_KEY=... or CLAUDE_CODE_OAUTH_TOKEN=... "
            f"(from `claude setup-token`) in {path} (chmod 600), or run with --mock.")
    return creds


class Referee:
    def __init__(self, cfg: GameConfig, *, resume=False):
        self.cfg = cfg
        self.resuming = resume
        self.lease = None
        self.transactions = []
        self.commits = {}
        self.rng = random.Random(cfg.seed) if cfg.seed is not None else random.SystemRandom()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.gid = cfg.game_id or f"{stamp}-{cfg.players}p-{cfg.driver}{'-mock' if cfg.mock else ''}-{secrets.token_hex(2)}"
        if Path(self.gid).name != self.gid or self.gid in (".", ".."):
            raise ValueError("Invalid game ID")
        self.dir = GAMES / self.gid
        self.ref = self.dir / "referee"
        self.turn_no = 0
        self.last_seen: dict[str, int] = {}
        self.status: dict = {"game_id": self.gid, "status": "starting", "created_at": now(), "referee_pid": os.getpid()}
        self.slot = SlotLock()
        self.drivers: dict = {}
        self.mock_proc = None
        self.monitor_task = None
        self.huddle_ctx: list[str] = []
        self.voice_procs: list = []
        self.bot_specs: dict[str, str] = {}     # local bot games: seat -> bot spec
        self.bot_labels: dict[str, str] = {}

    @classmethod
    def from_saved(cls, gid: str, *, max_turns=None):
        if Path(gid).name != gid or gid in (".", ".."):
            raise ValueError("Invalid game ID")
        path = GAMES / gid / "referee" / "recovery.json"
        if not path.exists():
            raise RuntimeError("This game predates durable recovery, or setup never finished. Its original files are preserved.")
        initial = json.loads(path.read_text())
        if initial.get("version") != 1:
            raise RuntimeError("Unsupported recovery version")
        cfg = GameConfig(**initial["config"])
        cfg.game_id, cfg.max_turns = gid, max_turns
        return cls(cfg, resume=True)

    # -- logging -------------------------------------------------------------
    def log(self, event: str, **kw):
        rec = {"ts": now(), "event": event, **kw}
        with open(self.ref / "referee.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")

    def save_status(self, **kw):
        self.status.update(kw, updated_at=now())
        tmp = self.ref / "status.json.tmp"
        tmp.write_text(json.dumps(self.status, indent=1))
        tmp.replace(self.ref / "status.json")

    # -- setup ---------------------------------------------------------------
    async def setup(self):
        if self.resuming:
            return await self.restore()
        cfg = self.cfg
        if cfg.driver == "claude" and (sys.platform != "linux" or os.geteuid() != 0):
            raise RuntimeError("The original Claude TUI backend requires its Linux root-managed seat sandbox.")
        n = cfg.players
        if n not in TEAM_SIZES:
            raise ValueError("Avalon requires 5–10 players")
        if not 1 <= cfg.max_rounds <= 10:
            raise ValueError("Discussion rounds must be between 1 and 10")
        role_keys = list(cfg.roles or PRESETS[n])
        if len(role_keys) != n:
            raise ValueError("The role list must match the player count")
        probs = validate_roles(role_keys)
        if probs:
            raise SystemExit("bad role setup: " + "; ".join(probs))
        players = [f"P{i}" for i in range(1, n + 1)]
        if cfg.assignment:
            if sorted(cfg.assignment) != sorted(players) or sorted(cfg.assignment.values()) != sorted(role_keys):
                raise ValueError("the fixed role assignment must give every seat one role of this table")
            self.assignment = dict(cfg.assignment)
        else:
            shuffled = list(role_keys)
            self.rng.shuffle(shuffled)
            self.assignment = dict(zip(players, shuffled))
        creds_real = load_credentials(cfg) if cfg.driver == "claude" and not cfg.mock else None
        if cfg.driver in ("scripted", "bots"):
            self.bot_specs, self.bot_labels = seat_plan(players, cfg.seats if cfg.driver == "bots" else None,
                                                        cfg.fill if cfg.driver == "bots" else "scripted", mock=cfg.mock)
        self.team_meta = team_seats(self.bot_specs)
        self.role_keys = role_keys
        self.rules_text = prompts.rules_md(players, role_keys, TEAM_SIZES[n], n >= 7, cfg.max_rounds, cfg.huddle_rounds)
        self.dir.mkdir(parents=True, mode=0o711, exist_ok=False)
        self.ref.mkdir(mode=0o700)
        self.lease = GameLease(self.ref / "referee.lock")
        self.lease.acquire()
        slot = self.slot.acquire()

        for d, mode in [(self.dir, 0o711), (self.dir / "players", 0o711), (self.ref, 0o700),
                        (self.ref / "screens", 0o700), (self.ref / "audit", 0o700)]:
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, mode)
        self.save_status(status="setup", slot=slot, players=players, driver=cfg.driver, model=cfg.model,
                         mock=cfg.mock, config=asdict(cfg))
        (self.ref / "roles.json").write_text(json.dumps(self.assignment, indent=1))
        os.chmod(self.ref / "roles.json", 0o600)

        self.game = Game(self.assignment, first_leader=cfg.first_leader, max_rounds=cfg.max_rounds,
                         huddle_rounds=cfg.huddle_rounds, reflect=cfg.reflect, rng=self.rng)

        seat_cfg = SeatConfig(model=cfg.model, effort=cfg.effort, allow_bash=cfg.allow_bash,
                              hook_guard=cfg.hook_guard)
        if cfg.driver == "claude":
            if cfg.mock:
                port = 18080 + slot
                seat_cfg.tcp_ports = [port]
                creds = {"ANTHROPIC_API_KEY": "sk-ant-api03-mock-" + secrets.token_hex(24),
                         "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}"}
                self.start_mock(port)
            else:
                creds = creds_real
        else:
            creds = {}
        seat_type = CodexSeat if cfg.driver == "codex" else LocalSeat if self.bot_specs else Seat
        self.seats = {p: seat_type(self.dir, p, slot, seat_cfg) for p in players}

        # Public repo first (seats reference its path).
        sizes = TEAM_SIZES[n]
        self.public = PublicRepo(self.dir / "public")
        game_info = {"game_id": self.gid, "players": players, "roles_in_play": roles_in_play_summary(role_keys),
                     "quest_team_sizes": sizes, "fails_required": [fails_required(n, i) for i in range(5)],
                     "max_discussion_rounds": cfg.max_rounds, "word_limit": prompts.WORD_LIMIT}
        rules = prompts.rules_md(players, role_keys, sizes, n >= 7, cfg.max_rounds, cfg.huddle_rounds)
        self.public.init(rules, game_info, self.public_state())
        os.chmod(self.dir / "public", 0o755)

        self.canaries = {}
        for p, seat in self.seats.items():
            info = knowledge(p, self.assignment)
            canary = "AVCANARY-" + secrets.token_hex(8)
            self.canaries[p] = canary
            role_md = prompts.role_card(p, info, role_keys) + f"\n<!-- integrity token (ignore): {canary} -->\n"
            role = ROLES[self.assignment[p]]
            claude_md = prompts.claude_md(p, role.name, role.side, seat.work, seat.public, n,
                                          sum(ROLES[r].side == EVIL for r in role_keys))
            seat.provision(claude_md, role_md, creds)
        (self.ref / "canaries.json").write_text(json.dumps(self.canaries, indent=1))
        (self.ref / "seats.json").write_text(json.dumps({
            p: {"uid": s.uid, "session_id": s.session_id, "dir": str(s.dir), "launch": str(s.launch_script),
                **({"bot": self.bot_labels[p]} if self.bot_labels else {}),
                **({"team": self.team_meta[p]} if p in self.team_meta else {})}
            for p, s in self.seats.items()}, indent=1))
        self.log("setup", players=players, roles=self.assignment, slot=slot, driver=cfg.driver)

        write_json(self.ref / "recovery.json", {
            "version": 1, "config": asdict(cfg), "assignment": self.assignment,
            "initial_leader": self.game.leader, "slot": slot,
            "seat_session_ids": {p: seat.session_id for p, seat in self.seats.items()},
            "created_at": self.status["created_at"],
        }, once=True)
        await self.start_players()

    async def start_players(self):
        cfg = self.cfg
        if self.game.phase == OVER:
            return
        if cfg.mock and any(s.startswith(("model:", "team:")) for s in self.bot_specs.values()):
            self.start_arena_mock()
        if cfg.driver == "claude":
            self.tmux = Tmux(self.ref / "tmux.sock", self.ref / "tmux.conf")
            self.drivers = {p: ClaudeTUIDriver(s, self.tmux, self.log) for p, s in self.seats.items()}
        elif cfg.driver == "codex":
            if cfg.mock:
                raise ValueError("Use the scripted driver for local mock games; Codex mode runs the requested real model.")
            self.drivers = {p: CodexDriver(s, self.log) for p, s in self.seats.items()}
        else:
            self.drivers = {p: self.local_driver(p, s) for p, s in self.seats.items()}
        self.save_status(status="starting_players")
        await asyncio.gather(*(d.start() for d in self.drivers.values()))
        if cfg.driver in ("claude", "codex") or any(s.startswith(("model:", "team:")) for s in self.bot_specs.values()):
            self.monitor_task = asyncio.create_task(self.monitor())
        self.save_status(status="running", recoverable=True)

    def local_driver(self, p: str, seat):
        rng = random.Random(self.rng.random())
        if self.bot_specs[p] == "scripted":
            return ScriptedDriver(seat, knowledge(p, self.assignment), self.log, rng)
        if self.bot_specs[p].startswith("team:"):
            from .tournament.player import PromptDriver
            from .tournament.teams import load_team
            spec, info = get_model(self.cfg.team_model or "haiku-5.5-or"), knowledge(p, self.assignment)
            base = self.mock_url + ("/v1" if spec.backend == "openai" else "") if self.cfg.mock else None
            return PromptDriver(seat, spec, load_team(self.bot_specs[p].removeprefix("team:")), info, self.game.players,
                                self.rules_text, prompts.role_card(p, info, self.role_keys), self.log,
                                self.huddle_entries, rng, base_url=base)
        if self.bot_specs[p].startswith("model:"):
            spec, info = get_model(self.bot_specs[p].removeprefix("model:")), knowledge(p, self.assignment)
            base = None
            if self.cfg.mock:
                base = self.mock_url + ("/v1" if spec.backend == "openai" else "")
            return ModelDriver(seat, spec, info, self.game.players, self.rules_text,
                               prompts.role_card(p, info, self.role_keys), self.log, self.huddle_entries, base_url=base)
        return bot_driver(seat, self.bot_specs[p], knowledge(p, self.assignment), self.game.players, rng,
                          self.log, self.huddle_entries)

    def huddle_entries(self) -> list[dict]:
        """The Evil huddle so far, for bots taking an Evil-only turn."""
        return [{"round": e["round"], **x} for txn in self.transactions for e in txn["events"]
                if e["type"] == "evil_huddle" for x in e["entries"]]

    async def restore(self):
        if self.cfg.driver != "codex":
            raise RuntimeError("Referee resume currently supports local Codex games. Original Claude in-session restart remains available.")
        self.lease = GameLease(self.ref / "referee.lock")
        self.lease.acquire()
        initial = json.loads((self.ref / "recovery.json").read_text())
        saved_status = json.loads((self.ref / "status.json").read_text())
        if saved_status.get("status") == "finished":
            # A completed run is immutable; reopening a replay needs no referee.
            self.lease.release()
            raise RuntimeError("This game already finished; open its replay instead")
        self.status = saved_status
        self.status.pop("error", None)
        self.assignment = initial["assignment"]
        if json.loads((self.ref / "roles.json").read_text()) != self.assignment:
            raise RuntimeError("Role file differs from the initial recovery record")
        self.canaries = json.loads((self.ref / "canaries.json").read_text())
        slot = self.slot.acquire(initial["slot"])
        self.game = Game(self.assignment, first_leader=initial["initial_leader"], max_rounds=self.cfg.max_rounds,
                         huddle_rounds=self.cfg.huddle_rounds, reflect=self.cfg.reflect)
        seat_cfg = SeatConfig(model=self.cfg.model, effort=self.cfg.effort, allow_bash=self.cfg.allow_bash,
                              hook_guard=self.cfg.hook_guard)
        self.seats = {p: CodexSeat(self.dir, p, slot, seat_cfg, session_id=initial["seat_session_ids"][p])
                      for p in self.game.players}
        for seat in self.seats.values():
            await CodexRPC.stop_orphan(seat.audit / "codex-server.log", seat.work / "scratch")
        self.public = PublicRepo(self.dir / "public")
        self.save_status(status="recovering", referee_pid=os.getpid(), resumed_at=now())
        for path in sorted((self.ref / "transactions").glob("[0-9]*.json")):
            txn = json.loads(path.read_text())
            turn = txn["turn"]
            if turn != self.turn_no + 1 or txn["kind"] != self.game.phase or txn["actors"] != self.game.actors():
                raise RuntimeError(f"Recovery transaction order disagrees with engine at turn {turn}")
            events = self.game.apply(txn["actions"])
            self.turn_no = turn
            if events != txn["events"] or self.public_state() != txn["state"]:
                raise RuntimeError(f"Recovery replay disagrees with the recorded turn {turn}")
            self.transactions.append(txn)
            self.accept_context(txn)
            self.commits[turn] = self.public.publish(turn, txn["kind"], events, txn["state"])
        self.sync_journals()
        self.log("resumed", turn=self.turn_no, pending_turn=self.turn_no + 1)
        await self.start_players()

    def accept_context(self, txn):
        for p in txn["actors"]:
            self.last_seen[p] = txn["turn"]
        for e in txn["events"]:
            if e["type"] == "evil_huddle":
                for x in e["entries"]:
                    if x["message"] or x["suggested_target"]:
                        self.huddle_ctx.append(f"- **{x['player']}** (round {e['round']}): {x['message'] or ''}"
                                               + (f" -> suggests {x['suggested_target']}" if x['suggested_target'] else ""))

    def sync_journals(self):
        events, turns = [], []
        for txn in self.transactions:
            events.extend({"turn": txn["turn"], "ts": txn["record"]["closed_at"], **e} for e in txn["events"])
            turns.append({**txn["record"], "commit": self.commits[txn["turn"]]})
        sync_jsonl(self.ref / "events.jsonl", events)
        sync_jsonl(self.ref / "turns.jsonl", turns)
        write_json(self.ref / "checkpoint.json", {"version": 1, "turn": self.turn_no,
                   "last_commit": self.commits.get(self.turn_no), "phase": self.game.phase})

    def start_mock(self, port: int):
        args = [sys.executable, "-m", "avalon.mockapi", "--port", str(port), "--log", str(self.ref / "mockapi.log"),
                "--latency", str(self.cfg.mock_latency)]
        if self.cfg.mock_adversarial:
            args.append("--adversarial")
        self.mock_proc = subprocess.Popen(args, cwd=str(ROOT), stdout=open(self.ref / "mockapi.out", "w"),
                                          stderr=subprocess.STDOUT)
        time.sleep(0.8)

    def start_arena_mock(self):
        """Stand-in models for every backend (HTTP APIs in a thread, CLIs as scripts). Dry runs only."""
        import threading
        from .arena_mock import serve
        self.mock_server = serve(0)
        threading.Thread(target=self.mock_server.serve_forever, daemon=True).start()
        self.mock_url = f"http://127.0.0.1:{self.mock_server.server_address[1]}"
        os.environ["AVALON_CODEX_BINARY"] = str(ROOT / "avalon" / "mock_clis" / "codex")
        os.environ["AVALON_CLAUDE_BINARY"] = str(ROOT / "avalon" / "mock_clis" / "claude")
        self.log("arena_mock", url=self.mock_url)

    def public_state(self) -> dict:
        st = self.game.public_state() if hasattr(self, "game") else {}
        return {"game_id": self.gid, "next_turn": self.turn_no + 1, "players": sorted(self.assignment, key=lambda p: int(p[1:])),
                **st}

    # -- monitoring ------------------------------------------------------------
    async def monitor(self):
        """Mirror every player's terminal and process facts for the dashboard."""
        while True:
            procs = {}
            for p, d in self.drivers.items():
                try:
                    scr = await d.screen()
                    (self.ref / "screens" / f"{d.seat.name}.ansi").write_text(scr or "")
                    pid = await d.pid()
                    procs[p] = ({"pid": pid, "backend": "codex", "boundary_checks": d.boundary_checks}
                                if isinstance(d, CodexDriver) else {"model_backend": d.spec.backend, "model": d.spec.model}
                                if isinstance(d, ModelDriver) else proc_facts(pid))
                except Exception as e:  # keep monitoring other seats
                    procs[p] = {"error": repr(e)}
            (self.ref / "procs.json").write_text(json.dumps(procs, indent=1))
            await asyncio.sleep(2.0)

    # -- turns -----------------------------------------------------------------
    def build_spec(self, turn: int, p: str) -> dict:
        seat = self.seats[p]
        f = self.game.turn_fields(p)
        pub = seat.public
        if f["kind"] == "onboard":
            reads = [str(pub / "RULES.md"), str(pub / "state.json")]
        else:
            since = self.last_seen.get(p, 0)
            new = [str(pub / "updates" / f"{t:04d}.md") for t in range(since + 1, turn)
                   if (pub / "updates" / f"{t:04d}.md").exists()]
            reads = [str(pub / "state.json")] + new[-8:]
        name = f"turn_{turn:04d}"
        return {"open": True, "turn": turn, "you": p, "players": sorted(self.assignment, key=lambda x: int(x[1:])),
                "instructions_name": "AGENTS.md" if self.cfg.driver == "codex" else "CLAUDE.md",
                **f, "read_first": reads, "public_dir": str(pub),
                "inbox_path": str(seat.work / "inbox" / f"{name}.md"),
                "notes_path": str(seat.work / "notes" / f"{name}.md"),
                "memory_path": str(seat.work / "memory.md"), "opened_at": now(),
                "evil_total": sum(ROLES[r].side == EVIL for r in self.assignment.values()),
                "submit_path": str(seat.work / "outbox" / f"{name}.json")}

    def private_context(self, p: str, spec: dict) -> list[str]:
        if spec["kind"] in (HUDDLE, ASSASSINATE) and self.huddle_ctx:
            return ["Evil huddle so far (only Evil players can see this):", *self.huddle_ctx]
        return []

    def write_inbox(self, p: str, spec: dict):
        seat = self.seats[p]
        md = prompts.turn_md(spec, self.game.public_state(), self.private_context(p, spec))
        for path, text in [(Path(spec["inbox_path"]), md), (seat.work / "inbox" / "current.json", json.dumps(spec))]:
            path.write_text(text)
            os.chmod(path, 0o644)

    def close_inbox(self, p: str, spec: dict):
        cur = self.seats[p].work / "inbox" / "current.json"
        cur.write_text(json.dumps({**spec, "open": False}))

    def default_action(self, p: str, spec: dict) -> dict:
        k, t = spec["kind"], spec["turn"]
        if k == "propose":
            order = self.game.players
            i = order.index(p)
            return {"turn": t, "team": sorted((order[(i + j) % len(order)] for j in range(spec["team_size"])),
                                              key=lambda x: int(x[1:])), "message": None}
        return {"onboard": {"ack": True}, "discuss": {"message": None, "ready_to_vote": True},
                "vote": {"vote": "approve"}, "quest": {"card": "success"},
                "evil_huddle": {"message": None, "suggested_target": None},
                "assassinate": {"target": self.rng.choice(spec.get("candidates") or self.game.players), "message": None},
                "reflect": {"message": None}}[k] | {"turn": t}

    def snapshot(self, turn: int, actors: list[str]):
        """Commit each actor's private folder to a referee-only audit repo."""
        for p in actors:
            seat = self.seats[p]
            gitdir = self.ref / "audit" / f"{seat.name}.git"
            base = ["git", "-c", "safe.directory=*", f"--git-dir={gitdir}", f"--work-tree={seat.work}"]
            env = {**os.environ, "GIT_AUTHOR_NAME": "referee", "GIT_AUTHOR_EMAIL": "referee@avalon.local",
                   "GIT_COMMITTER_NAME": "referee", "GIT_COMMITTER_EMAIL": "referee@avalon.local"}
            if not gitdir.exists():
                subprocess.run(["git", "init", "-q", "--bare", str(gitdir)], check=True)
            subprocess.run(base + ["add", "-A"], check=True, env=env, capture_output=True)
            subprocess.run(base + ["commit", "-q", "--allow-empty", "-m", f"turn {turn:04d}"], check=True,
                           env=env, capture_output=True)

    def turn_activity(self, p: str) -> dict:
        d = self.drivers[p]
        if hasattr(d, "activity"):            # Codex sessions and tournament prompt players
            return d.activity()
        if not isinstance(d, ClaudeTUIDriver):
            return {}
        evs = d.tail.events[d.turn_start_idx:]
        tools = [e for e in evs if e.get("event") == "PreToolUse"]
        reads = [pth for e in tools if e.get("tool") == "Read" for pth in e.get("paths", [])]
        return {"tool_calls": len(tools), "reads": reads,
                "guard_denials": sum(e.get("guard") == "deny" for e in tools),
                "stop_blocks": sum(e.get("event") == "Stop" and e.get("status") == "blocked" for e in evs),
                "tools": [e.get("tool") for e in tools]}

    async def play_turn(self) -> bool:
        actors = self.game.actors()
        if not actors:
            return False
        self.turn_no += 1
        turn, kind = self.turn_no, self.game.phase
        pending = self.ref / "pending" / f"{turn:04d}"
        request_path = pending / "request.json"
        if request_path.exists():
            request = json.loads(request_path.read_text())
            if request["kind"] != kind or request["actors"] != actors or request["turn"] != turn:
                raise RuntimeError("Pending turn disagrees with recovered game state")
            specs, opened = request["specs"], request["opened_at"]
        else:
            specs = {p: self.build_spec(turn, p) for p in actors}
            opened = now()
            write_json(request_path, {"version": 1, "turn": turn, "kind": kind,
                       "actors": actors, "specs": specs, "opened_at": opened}, once=True)
        for p in actors:
            self.write_inbox(p, specs[p])
        timeout = self.cfg.onboard_timeout if kind == "onboard" else self.cfg.turn_timeout
        deadline = now() + timeout
        receipts = {}
        for p in actors:
            path = pending / f"{p}.json"
            if path.exists():
                receipt = json.loads(path.read_text())
                errors, action = turnspec.validate_submission(specs[p], receipt["action"])
                if errors or action != receipt["action"] or receipt["turn"] != turn or receipt["player"] != p:
                    raise RuntimeError(f"Invalid saved receipt for {p} turn {turn}")
                receipts[p] = receipt
        self.save_status(phase=kind, turn=turn, actors=actors, opened_at=opened, deadline=deadline,
                         seats_done={p: {"status": "done", "at": r["accepted_at"]} for p, r in receipts.items()})
        self.log("turn_resumed" if receipts else "turn_open", turn=turn, kind=kind, actors=actors,
                 accepted_players=list(receipts))

        def prompt_for(p):
            text = prompts.onboard_prompt(self.seats[p].work, specs[p].get("instructions_name", "AGENTS.md" if self.cfg.driver == "codex" else "CLAUDE.md")) if kind == "onboard" else prompts.turn_prompt(specs[p])
            if self.resuming:
                text = f"Continue the same game. If turn {turn} was interrupted, finish its existing files; no moves from this round have been published. " + text
            return text

        waiting = [p for p in actors if p not in receipts]
        await asyncio.gather(*(self.drivers[p].open_turn(specs[p], prompt_for(p)) for p in waiting))

        async def one(p):
            result = await self.drivers[p].wait_done(specs[p], deadline,
                                                   lambda secs, spec=specs[p]: prompts.nudge_prompt(spec, secs))
            errors, action, note_words = turnspec.check_turn_files(specs[p])
            if result["status"] != "done" or errors or action is None:
                self.status.setdefault("seats_done", {})[p] = {"status": result["status"], "at": now(), "errors": errors}
                self.save_status()
                return {"player": p, "result": result, "errors": errors}
            receipt = {"version": 1, "turn": turn, "player": p, "action": action,
                       "accepted_at": now(), "done_at": (result.get("record") or {}).get("ts"),
                       "note_words": note_words, "activity": self.turn_activity(p),
                       "notes": Path(specs[p]["notes_path"]).read_text(),
                       "memory": Path(specs[p]["memory_path"]).read_text()}
            write_json(pending / f"{p}.json", receipt, once=True)
            receipts[p] = receipt
            self.status.setdefault("seats_done", {})[p] = {"status": "done", "at": receipt["accepted_at"]}
            self.save_status()
            return None

        failures = [r for r in await asyncio.gather(*(one(p) for p in waiting)) if r]
        if failures:
            raise RuntimeError("Turn remains unpublished. Accepted moves are saved; no heuristic/default replacements: " + json.dumps(failures))
        closed = now()
        for p in actors:
            self.close_inbox(p, specs[p])
        self.snapshot(turn, actors)
        actions = {p: receipts[p]["action"] for p in actors}
        per_seat = {p: {"status": "done", "defaulted": False, "errors": [], "note_words": r["note_words"],
                        "submission": r["action"], "done_at": r["done_at"] or r["accepted_at"],
                        "accepted_at": r["accepted_at"], **r["activity"]} for p, r in receipts.items()}
        events = self.game.apply(actions)
        record = {"turn": turn, "kind": kind, "actors": actors, "opened_at": opened,
                  "closed_at": closed, "quest": specs[actors[0]]["quest"],
                  "proposal": specs[actors[0]]["proposal"], "round": specs[actors[0]].get("round"), "seats": per_seat}
        txn = {"version": 1, "turn": turn, "kind": kind, "actors": actors,
               "actions": actions, "events": events, "state": self.public_state(), "record": record}
        # This is the commit decision: all accepted moves are durable before public I/O.
        write_json(self.ref / "transactions" / f"{turn:04d}.json", txn, once=True)
        self.transactions.append(txn)
        self.accept_context(txn)
        commit = self.public.publish(turn, kind, events, txn["state"])
        self.commits[turn] = commit
        self.sync_journals()
        if self.cfg.voice and any(e.get("visibility") == "public" and (e.get("message") or e.get("entries")) for e in events):
            from .voice import render_async
            self.voice_procs.append(render_async(self.gid, turn))
        self.log("turn_closed", turn=turn, kind=kind, commit=commit,
                 statuses={p: "done" for p in actors})
        self.save_status(phase=self.game.phase, last_commit=commit, score=self.game.score, winner=self.game.winner)
        return True

    async def run(self) -> Path:
        try:
            await self.setup()
            while True:
                if self.game.phase != OVER and self.cfg.max_turns and self.turn_no >= self.cfg.max_turns:
                    self.save_status(status="stopped", stopped_after=self.turn_no)
                    self.log("stopped", turn=self.turn_no)
                    return self.dir
                if not await self.play_turn():
                    break
            self.save_status(status="finished", phase=OVER, winner=self.game.winner,
                             win_reason=self.game.win_reason, finished_at=now())
            self.write_summary()
            return self.dir
        except BaseException as e:
            if self.lease and self.lease.fh:
                self.save_status(status="stopped" if isinstance(e, asyncio.CancelledError) else "error",
                                 error=repr(e), recoverable=(self.ref / "recovery.json").exists())
                self.log("error", error=repr(e))
            raise
        finally:
            await self.teardown()

    def write_summary(self):
        g = self.game
        summary = {
            "game_id": self.gid, "players": g.n, "driver": self.cfg.driver, "model": self.cfg.model,
            "mock": self.cfg.mock, "tag": self.cfg.tag, "winner": g.winner, "win_reason": g.win_reason,
            "roles": self.assignment, "score": g.score, "turns": self.turn_no,
            "quests": [asdict(r) for r in g.results],
            "proposals": [{"quest": p.quest, "number": p.number, "leader": p.leader, "team": p.team,
                           "approved": p.approved, "votes": p.votes, "rounds": len(p.rounds),
                           "amendments": len(p.amendments)} for p in g.proposals],
            "assassin_target": g.assassin_target,
            "merlin": g.role_holder("merlin"),
            "started_at": self.status.get("created_at"), "finished_at": now(),
        }
        if self.bot_labels:
            summary["bots"] = self.bot_labels
        if getattr(self, "team_meta", None):
            summary["teams"] = self.team_meta
            summary["team_model"] = self.cfg.team_model
        faults = {}
        for txn in self.transactions:
            for p, seat in txn["record"]["seats"].items():
                if seat.get("fault"):
                    faults.setdefault(p, []).append({"turn": txn["turn"], "kind": txn["kind"], "cause": seat["fault"]["cause"]})
        if faults:
            summary["faults"] = faults
        (self.ref / "summary.json").write_text(json.dumps(summary, indent=1))

    async def capture_screens(self):
        for d in self.drivers.values():
            try:
                scr = await d.screen()
                if scr:
                    (self.ref / "screens" / f"{d.seat.name}.ansi").write_text(scr)
            except Exception:
                pass

    async def teardown(self):
        if not self.lease or not self.lease.fh:
            return
        if self.monitor_task:
            self.monitor_task.cancel()
        if (self.cfg.driver in ("claude", "codex") or self.monitor_task) and self.drivers:
            await asyncio.sleep(1.0)
            await self.capture_screens()
        try:
            await asyncio.wait_for(asyncio.gather(*(d.stop() for d in self.drivers.values()),
                                                  return_exceptions=True), 30)
        except asyncio.TimeoutError:
            pass
        if self.cfg.driver == "claude" and hasattr(self, "tmux"):
            await self.tmux.run("kill-server", check=False)
        if self.mock_proc:
            self.mock_proc.terminate()
        if getattr(self, "mock_server", None):
            self.mock_server.shutdown()
            self.mock_server.server_close()
        self.slot.release()
        try:
            from .audit import audit_game
            report = audit_game(self.dir)
            (self.ref / "audit_report.json").write_text(json.dumps(report, indent=1))
        except Exception as e:
            self.log("audit_error", error=repr(e))
        finally:
            self.lease.release()


def team_seats(specs: dict[str, str]) -> dict[str, dict]:
    """Tournament seats ("team:<folder>"): the team's id, display name, color and prompt hash, by seat."""
    if not any(s.startswith("team:") for s in specs.values()):
        return {}
    from .tournament.teams import load_team
    return {p: load_team(s.removeprefix("team:")).meta() for p, s in specs.items() if s.startswith("team:")}


def proc_facts(pid: int | None) -> dict:
    """Read the player's process credentials and sandbox flags from /proc."""
    if not pid:
        return {}
    facts = {"pid": pid}
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            k, _, v = line.partition(":")
            if k in ("Uid", "Gid", "NoNewPrivs", "Seccomp", "Seccomp_filters", "VmRSS", "Threads", "State"):
                facts[k] = " ".join(v.split())
        facts["cmd"] = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[0].decode()
    except OSError as e:
        facts["error"] = str(e)
    return facts
