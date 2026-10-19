"""In-process Python bots (avalon.bot.Bot subclasses) on no-root local seats.

The adapter gives a bot its turn facts and the public record, then writes the
same three files every player writes (private notes, memory, submission), so
the referee validates and audits bot moves exactly like model moves.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import traceback
from pathlib import Path

from ..bot import Bot
from ..seat import HOOKS

sys.path.insert(0, str(HOOKS))
import turnspec  # noqa: E402

BUILTIN = {"scripted", "random"}
HIDDEN_SPEC_KEYS = {"open", "instructions_name", "read_first", "public_dir", "opened_at",
                    "inbox_path", "notes_path", "memory_path", "submit_path"}


class BotError(RuntimeError):
    """A bot raised, returned an invalid move, or could not be loaded."""


def resolve_spec(spec: str, base: str | os.PathLike | None = None) -> str:
    """Make a bot file path absolute (relative to `base`); builtin names pass through."""
    if spec in BUILTIN or spec.startswith(("model:", "team:")):
        return spec
    path, _, cls = spec.partition(":")
    full = Path(base or os.getcwd(), path).expanduser().resolve()
    return f"{full}:{cls}" if cls else str(full)


def load_bot(spec: str, player: str = "check") -> tuple[type[Bot], str]:
    """Return (Bot subclass, label) for 'random' or 'path/to/bot.py[:ClassName]'.

    Each seat gets its own copy of the module, so two seats running the same
    bot never share module-level state.
    """
    if spec == "random":
        return Bot, "random"
    path, _, cls_name = spec.partition(":")
    file = Path(path)
    if file.suffix != ".py" or not file.is_file():
        raise BotError(f"bot {spec!r}: expected 'scripted', 'random' or an existing .py file (optionally :ClassName)")
    if str(file.parent) not in sys.path:
        sys.path.insert(0, str(file.parent))      # lets a bot import helper modules next to it
    mod_name = f"avalon_bot_{player.lower()}_{file.stem}"
    mspec = importlib.util.spec_from_file_location(mod_name, file)
    module = importlib.util.module_from_spec(mspec)
    sys.modules[mod_name] = module
    try:
        mspec.loader.exec_module(module)
    except Exception as e:
        raise BotError(f"bot {file.name} failed to import: {type(e).__name__}: {e}\n{traceback.format_exc()}") from e
    if cls_name:
        cls = getattr(module, cls_name, None)
        if not (isinstance(cls, type) and issubclass(cls, Bot)):
            raise BotError(f"bot {file.name}: {cls_name} is not a subclass of avalon.bot.Bot")
    else:
        found = [c for c in vars(module).values()
                 if isinstance(c, type) and issubclass(c, Bot) and c.__module__ == mod_name]
        if len(found) != 1:
            names = ", ".join(c.__name__ for c in found) or "none"
            raise BotError(f"bot {file.name} defines {len(found)} Bot subclasses ({names}); "
                           f"name one with {file.name}:ClassName")
        cls = found[0]
    return cls, cls.name or f"{file.stem}:{cls.__name__}"


def model_label(spec: str, mock: bool = False) -> str:
    """Label for a 'model:<key>' seat; refuses models that cannot play here (unless mocked)."""
    from ..models import ModelError, get_model, load_models, preflight
    key = spec.removeprefix("model:")
    try:
        m = get_model(key)
    except ModelError as e:
        raise BotError(str(e)) from e
    problem = None if mock else preflight(m)
    if problem:
        same = [k for k, o in load_models().items() if o.label == m.label and k != key and not preflight(o)]
        hint = f" The same model is ready here as {', '.join(same)}." if same else ""
        raise BotError(f"cannot seat {m.label}: {problem}.{hint} Use --mock for a dry run with stand-in models.")
    return m.label


def team_label(spec: str) -> str:
    """Label for a 'team:<folder>' tournament seat: the team's display name (checks the submission)."""
    from ..tournament.teams import TeamError, load_team
    try:
        return load_team(spec.removeprefix("team:")).name
    except TeamError as e:
        raise BotError(str(e)) from e


def seat_plan(players: list[str], seats: dict[str, str] | None, fill: str,
              mock: bool = False) -> tuple[dict[str, str], dict[str, str]]:
    """(spec, label) for every seat: the --seat choices, the rest filled with `fill`.

    Loads each bot (and checks each model) once, so a broken seat fails before any game files exist.
    """
    seats = seats or {}
    unknown = sorted(set(seats) - set(players), key=lambda p: (len(p), p))
    if unknown:
        raise ValueError(f"--seat names {', '.join(unknown)}, but this game's seats are {players[0]}-{players[-1]}")
    plan = {p: seats.get(p, fill) for p in players}
    labels = {spec: spec if spec == "scripted" else model_label(spec, mock) if spec.startswith("model:")
              else team_label(spec) if spec.startswith("team:") else load_bot(spec)[1] for spec in set(plan.values())}
    return plan, {p: labels[spec] for p, spec in plan.items()}


def bot_driver(seat, spec: str, knowledge: dict, players: list[str], rng, log, huddle) -> "BotDriver":
    cls, label = load_bot(spec, seat.player)
    try:
        bot = cls(seat.player, knowledge, players, rng)
    except Exception as e:
        raise BotError(f"{seat.player} ({label}) raised {type(e).__name__} while starting: {e}\n"
                       f"{traceback.format_exc()}") from e
    return BotDriver(seat, bot, label, log, huddle)


class BotDriver:
    kind = "bot"

    def __init__(self, seat, bot: Bot, label: str, log, huddle=lambda: []):
        self.seat, self.bot, self.label, self.log, self.huddle = seat, bot, label, log, huddle
        self.last = None

    @property
    def player(self) -> str:
        return self.seat.player

    async def start(self):
        self.log("seat_started", player=self.player, pid=os.getpid(), session_id=None, bot=self.label)

    async def open_turn(self, spec, prompt):
        kind, who = spec["kind"], f"{self.player} ({self.label})"
        turn = {k: v for k, v in spec.items() if k not in HIDDEN_SPEC_KEYS}
        if kind in ("evil_huddle", "assassinate"):
            turn["huddle"] = self.huddle()
        public = self.seat.public
        state = json.loads((public / "state.json").read_text())
        state["events"] = [json.loads(line) for line in (public / "events.jsonl").read_text().splitlines() if line.strip()]
        try:
            move = self.bot.act(turn, state)
            if not isinstance(move, dict):
                raise TypeError(f"{kind}() must return a dict, got {type(move).__name__}")
            move = dict(move)
            reason = move.pop("reason", None)
            move["turn"] = spec["turn"]
            if kind in turnspec.BELIEF_KINDS and "beliefs" not in move:
                move["beliefs"] = self.bot.beliefs(turn, state)
            memory = self.bot.memory()
        except Exception as e:
            tb = traceback.format_exc()
            with (self.seat.audit / "bot-errors.log").open("a") as f:
                f.write(f"turn {spec['turn']} ({kind})\n{tb}\n")
            raise BotError(f"{who} raised {type(e).__name__} on turn {spec['turn']} ({kind}): {e}\n{tb}") from e
        errors, action = turnspec.validate_submission(spec, move)
        if errors:
            raise BotError(f"{who} made an invalid {kind} move on turn {spec['turn']}: {'; '.join(errors)}\n"
                           f"move: {json.dumps(move)}")
        notes = (f"# Turn {spec['turn']} ({kind}): {self.player} as {self.bot.role}, played by {self.label}\n\n"
                 f"Reason: {reason or '(the bot gave no reason)'}\n\n"
                 f"Move: {json.dumps(action, sort_keys=True)}\n\n"
                 f"This private decision record was written by the local bot adapter from the bot's own reason and move.\n")
        if not (memory or "").strip():
            memory = (f"# Memory\n\nTurn {spec['turn']} ({kind}): {reason or 'no reason given'}\n"
                      f"Beliefs: {json.dumps(action.get('beliefs', {}), sort_keys=True)}\n")
        for path, text in [(spec["notes_path"], notes), (spec["memory_path"], memory),
                           (spec["submit_path"], json.dumps(move, indent=2))]:
            Path(path).write_text(text)
        self.last = {"ts": time.time(), "event": "turn_done", "turn": spec["turn"], "valid": True, "submission": action}

    async def wait_done(self, spec, deadline, nudge_text):
        return {"status": "done", "record": self.last}

    async def screen(self, ansi=True, history=80):
        return None

    async def pid(self):
        return None

    async def stop(self):
        pass
