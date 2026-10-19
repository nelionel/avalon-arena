"""The model arena: balanced seatings across many games, and a leaderboard by model (or bot).

  bin/avalon arena --models haiku-5.5,gpt-6-luna,qwen-3.8-flash-next --players 6 --games 4
  bin/avalon leaderboard
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from collections import defaultdict
from pathlib import Path

from .models import get_model
from .roles import ROLES, validate_roles

DEFAULT_MODELS = "haiku-5.5,gpt-6-luna,qwen-3.8-flash-next"


def entrant(name: str) -> str:
    """Seat spec for an arena entrant: a models.toml key, or a bot ('scripted', 'random', path/to/bot.py[:Class])."""
    from .drivers.local_bot import BUILTIN, resolve_spec
    from .models import load_models
    if name in load_models():
        return f"model:{name}"
    if name in BUILTIN or name.split(":")[0].endswith(".py"):
        return resolve_spec(name, os.environ.get("AVALON_CWD"))
    get_model(name)                     # raises: unknown model, listing the configured ones
    return name


def seatings(entrants: list[str], players: int, games: int, seed: int | None) -> list[dict[str, str]]:
    """Every entrant gets as many seats as possible, the extra seats rotate between entrants, and seats are shuffled."""
    rng = random.Random(seed)
    specs = [entrant(e) for e in entrants]
    out = []
    for g in range(games):
        pool = [specs[(g + i) % len(specs)] for i in range(players)]
        rng.shuffle(pool)
        out.append({f"P{i + 1}": s for i, s in enumerate(pool)})
    return out


async def run_arena(models: list[str], players: int, games: int, *, parallel: int = 1, seed: int | None = None,
                    tag: str | None = None, mock: bool = False, timeout: float = 900, max_rounds: int = 3,
                    huddle_rounds: int = 1, reflect: bool = True, roles: list[str] | None = None) -> tuple[str, list[str]]:
    """Play the series; returns (tag, game ids). Every entrant and the role list are checked before any game starts."""
    from .drivers.local_bot import load_bot, model_label
    from .referee import GameConfig, Referee
    if roles:
        problems = validate_roles(roles) if len(roles) == players else [f"{len(roles)} roles for {players} players"]
        if problems:
            raise ValueError("--roles: " + "; ".join(problems))
    plans = seatings(models, players, games, seed)
    for spec in {s for plan in plans for s in plan.values()}:
        if spec.startswith("model:"):
            model_label(spec, mock)          # raises BotError naming what is missing
        elif spec != "scripted":
            load_bot(spec)
    tag = tag or f"arena-{time.strftime('%Y%m%d-%H%M')}"
    sem, done = asyncio.Semaphore(parallel), []

    async def one(i, seats):
        async with sem:
            cfg = GameConfig(players=players, roles=roles, driver="bots", seats=seats, mock=mock, tag=tag, voice=False,
                             seed=None if seed is None else seed + i, turn_timeout=timeout,
                             onboard_timeout=max(timeout, 420), max_rounds=max_rounds, huddle_rounds=huddle_rounds,
                             reflect=reflect)
            r = Referee(cfg)
            print(f"game {i + 1}/{games} {r.gid}: " + ", ".join(f"{p} {s.removeprefix('model:').split('/')[-1]}"
                                                               for p, s in seats.items()), flush=True)
            try:
                await r.run()
                s = json.loads((r.ref / "summary.json").read_text())
                print(f"game {i + 1}/{games} {r.gid}: {s['winner'].upper()} wins ({s['win_reason']})", flush=True)
            except Exception as e:      # keep the series going; the game keeps its saved state
                print(f"game {i + 1}/{games} {r.gid}: stopped: {str(e)[:400]}", flush=True)
            done.append(r.gid)

    await asyncio.gather(*(one(i, s) for i, s in enumerate(plans)))
    return tag, done


def _jsonl(path: Path) -> list[dict]:
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except OSError:
        return []


def compute_leaderboard(games_dir: Path | None = None, *, tag: str | None = None, include_mock: bool = False) -> dict:
    """Results by player label (model or bot) over finished local-bot games."""
    from .referee import GAMES
    games_dir = games_dir or GAMES
    rows: dict = defaultdict(lambda: defaultdict(float))
    counted, stopped = 0, 0
    for d in sorted(games_dir.iterdir()) if games_dir.exists() else []:
        try:
            st = json.loads((d / "referee" / "status.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        cfg = st.get("config") or {}
        if cfg.get("driver") != "bots" or (cfg.get("mock") and not include_mock) or (tag and cfg.get("tag") != tag) \
                or cfg.get("team_model"):               # tournament games have their own standings
            continue
        if st.get("status") == "error":
            stopped += 1
            for label in set(re.findall(r"P\d+ \(([^)]+)\)", st.get("error") or "")):
                rows[label]["stopped_games"] += 1
            continue
        try:
            s = json.loads((d / "referee" / "summary.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        counted += 1
        final_beliefs = {}
        for t in _jsonl(d / "referee" / "turns.jsonl"):
            for p, seat in (t.get("seats") or {}).items():
                b = (seat.get("submission") or {}).get("beliefs")
                if b:
                    final_beliefs[p] = b
        sides = {q: ROLES[r].side for q, r in s["roles"].items()}
        for p, label in (s.get("bots") or {}).items():
            role = s["roles"][p]
            side = ROLES[role].side
            won = s["winner"] == side
            r = rows[label]
            r["seats"] += 1
            r["wins"] += won
            r[f"{side}_seats"] += 1
            r[f"{side}_wins"] += won
            b = final_beliefs.get(p)
            if side == "good" and role != "merlin" and b:     # Merlin already knows; reads only count for the others
                ev = [v for q, v in b.items() if sides.get(q) == "evil"]
                gd = [v for q, v in b.items() if sides.get(q) == "good"]
                if ev and gd:
                    r["read_sum"] += sum(ev) / len(ev) - sum(gd) / len(gd)
                    r["read_n"] += 1
            if role == "merlin" and s.get("assassin_target"):   # survival only means something after a shot
                r["merlin_games"] += 1
                r["merlin_survived"] += s["win_reason"] != "merlin_assassinated"
            if role == "assassin" and s.get("assassin_target"):
                r["assassin_shots"] += 1
                r["assassin_hits"] += s["win_reason"] == "merlin_assassinated"
            usage = _jsonl(d / "players" / p.lower() / "audit" / "usage.jsonl")
            if any("cost" in u for u in usage):
                r["cost_seats"] += 1
            for u in usage:
                r["cost"] += u.get("cost") or 0
                r["calls"] += 1
                r["seconds"] += u.get("seconds") or 0
                r["retries"] += (u.get("attempt") or 1) > 1
                r["tokens_in"] += u.get("input") or 0
                r["tokens_out"] += u.get("output") or 0
    labels = {}
    for label, r in rows.items():
        pct = lambda a, b: round(r[a] / r[b], 3) if r[b] else None
        labels[label] = {
            "seats": int(r["seats"]), "wins": int(r["wins"]), "win_rate": pct("wins", "seats"),
            "good": {"seats": int(r["good_seats"]), "win_rate": pct("good_wins", "good_seats")},
            "evil": {"seats": int(r["evil_seats"]), "win_rate": pct("evil_wins", "evil_seats")},
            "merlin": {"games": int(r["merlin_games"]), "survival": pct("merlin_survived", "merlin_games")},
            "assassin": {"shots": int(r["assassin_shots"]), "hit_rate": pct("assassin_hits", "assassin_shots")},
            "evil_read": round(r["read_sum"] / r["read_n"], 2) if r["read_n"] else None,
            "seconds_per_move": round(r["seconds"] / r["calls"], 1) if r["calls"] else None,
            "retry_rate": pct("retries", "calls"), "stopped_games": int(r["stopped_games"]),
            "tokens": {"in": int(r["tokens_in"]), "out": int(r["tokens_out"])},
            "cost_usd": round(r["cost"], 4) if r["cost_seats"] else None,
            "cost_per_seat_game": round(r["cost"] / r["cost_seats"], 4) if r["cost_seats"] else None,
        }
    order = sorted(labels, key=lambda k: (-(labels[k]["win_rate"] or 0), -labels[k]["seats"], k))
    return {"filters": {"tag": tag, "include_mock": include_mock}, "games": counted, "stopped_games": stopped,
            "players": [{"label": k, **labels[k]} for k in order], "colors": _colors()}


def _colors() -> dict:
    from .models import load_models
    return {m.label: m.color for m in load_models().values()}


def format_leaderboard(lb: dict) -> str:
    pct = lambda v: "  -  " if v is None else f"{v * 100:4.0f}%"
    lines = [f"{lb['games']} finished game(s)" + (f", {lb['stopped_games']} stopped" if lb["stopped_games"] else ""),
             f"{'player':22} {'seats':>5} {'win':>6} {'good':>6} {'evil':>6} {'read':>5} {'merlin':>7} {'shot':>6} {'s/move':>7} {'retry':>6} {'$/game':>7}"]
    for r in lb["players"]:
        lines.append(f"{r['label'][:22]:22} {r['seats']:>5} {pct(r['win_rate']):>6} {pct(r['good']['win_rate']):>6} "
                     f"{pct(r['evil']['win_rate']):>6} {'-' if r['evil_read'] is None else r['evil_read']:>5} {pct(r['merlin']['survival']):>7} {pct(r['assassin']['hit_rate']):>6} "
                     f"{'-' if r['seconds_per_move'] is None else r['seconds_per_move']:>7} {pct(r['retry_rate']):>6} "
                     f"{'-' if r['cost_per_seat_game'] is None else '%.3f' % r['cost_per_seat_game']:>7}")
    return "\n".join(lines)
