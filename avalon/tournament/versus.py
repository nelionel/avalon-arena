"""Head-to-head: two prompts on opposite sides of one table.

  bin/avalon versus teams/a teams/b --games 16 --model haiku-5.5-or --parallel 4 --seed 7
  bin/avalon versus --report <tag>

Games come in pairs that share the seats, the role deal and the first leader. In the first game of a pair, A
plays every Good seat (Merlin, Percival, 3 Loyal Servants) and B every Evil seat (Morgana, the Assassin,
Oberon); in the second they swap. Every game is one A-against-B trial and each prompt plays each side equally
often, so A's share of the wins estimates how often A beats B. The report gives that share with a 95% Wilson
interval.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import time
from pathlib import Path

from ..roles import ROLES
from .series import TEAMS, TOURNAMENT_ROLES
from .teams import Team

OTHER = {"a": "b", "b": "a"}


def pairs(games: int, seed: int | None) -> list[dict]:
    """[{"roles": {"P1": role, ...}, "good": "a" | "b"}, ...]: each pair shares a deal, A is Good in its first game."""
    if games < 2 or games % 2:
        raise ValueError(f"--games must be even: games come in pairs with the sides swapped (got {games})")
    rng, plans = random.Random(seed), []
    for _ in range(games // 2):
        roles = {f"P{i + 1}": r for i, r in enumerate(rng.sample(TOURNAMENT_ROLES, TEAMS))}
        plans += [{"roles": roles, "good": "a"}, {"roles": roles, "good": "b"}]
    return plans


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    """95% Wilson score interval for k successes in n trials."""
    if not n:
        return None, None
    p, d = k / n, 1 + z * z / n
    centre, half = (p + z * z / (2 * n)) / d, z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)


async def run_versus(a: Team, b: Team, games: int, *, model: str, parallel: int = 1, seed: int | None = None,
                     tag: str | None = None, mock: bool = False, timeout: float = 600, max_rounds: int = 3,
                     reflect: bool = True) -> tuple[str, list[str]]:
    """Play the pairs; returns (tag, game ids). A game that stops is reported and the series goes on."""
    from ..drivers.local_bot import model_label
    from ..referee import GAMES, GameConfig, Referee
    model_label(f"model:{model}", mock)          # raises, naming what is missing, before any game starts
    seed = seed if seed is not None else random.SystemRandom().randrange(10 ** 9)
    plans = pairs(games, seed)
    tag = tag or f"versus-{time.strftime('%Y%m%d-%H%M')}"
    GAMES.mkdir(parents=True, exist_ok=True)
    manifest = GAMES / f".{tag}.versus.json"
    record = {"tag": tag, "model": model, "seed": seed, "mock": mock, "games": games, "a": a.meta(), "b": b.meta(),
              "schedule": plans, "game_ids": [None] * games}
    manifest.write_text(json.dumps(record, indent=1))
    sides, sem, done = {"a": a, "b": b}, asyncio.Semaphore(parallel), []

    async def one(i, plan):
        good, evil = sides[plan["good"]], sides[OTHER[plan["good"]]]
        seats = {p: f"team:{(good if ROLES[r].side == 'good' else evil).path}" for p, r in plan["roles"].items()}
        async with sem:
            cfg = GameConfig(players=TEAMS, roles=TOURNAMENT_ROLES, driver="bots", mock=mock, tag=tag, voice=False,
                             seed=seed + i // 2, seats=seats, assignment=plan["roles"], team_model=model,
                             turn_timeout=timeout, onboard_timeout=max(timeout, 420), max_rounds=max_rounds,
                             reflect=reflect)
            r = Referee(cfg)
            record["game_ids"][i] = r.gid
            manifest.write_text(json.dumps(record, indent=1))
            print(f"game {i + 1}/{games} {r.gid}: Good {good.name}, Evil {evil.name}", flush=True)
            try:
                await r.run()
                s = json.loads((r.ref / "summary.json").read_text())
                winner = good if s["winner"] == "good" else evil
                print(f"game {i + 1}/{games} {r.gid}: {winner.name} wins as {s['winner'].title()} ({s['win_reason']})",
                      flush=True)
            except Exception as e:      # keep the series going; the game keeps its saved state
                print(f"game {i + 1}/{games} {r.gid}: stopped: {str(e)[:400]}", flush=True)
            done.append(r.gid)

    await asyncio.gather(*(one(i, p) for i, p in enumerate(plans)))
    return tag, done


def versus_tags(games_dir: Path | None = None) -> list[str]:
    from ..referee import GAMES
    d = games_dir or GAMES
    found = sorted(d.glob(".*.versus.json"), key=lambda p: p.stat().st_mtime) if d.exists() else []
    return [p.name[1:-len(".versus.json")] for p in found]


def compute_versus(tag: str | None = None, games_dir: Path | None = None) -> dict:
    """Wins by prompt and side over the finished games of one head-to-head series."""
    from ..referee import GAMES
    games_dir = games_dir or GAMES
    tag = tag or (versus_tags(games_dir) or [None])[-1]
    if not tag or not (games_dir / f".{tag}.versus.json").exists():
        raise ValueError(f"no head-to-head series {tag!r}; known: {', '.join(versus_tags(games_dir)) or 'none'}")
    rec = json.loads((games_dir / f".{tag}.versus.json").read_text())
    won = {k: {"good": 0, "evil": 0} for k in "ab"}
    played = {k: {"good": 0, "evil": 0} for k in "ab"}
    stopped, games = 0, []
    for plan, gid in zip(rec["schedule"], rec["game_ids"]):
        if not gid:
            continue
        try:
            st = json.loads((games_dir / gid / "referee" / "status.json").read_text())
            s = json.loads((games_dir / gid / "referee" / "summary.json").read_text()) if st.get("status") == "finished" else None
        except (OSError, json.JSONDecodeError):
            s = None
        if not s:
            stopped += 1
            continue
        good = plan["good"]
        winner = good if s["winner"] == "good" else OTHER[good]
        played[good]["good"] += 1
        played[OTHER[good]]["evil"] += 1
        won[winner][s["winner"]] += 1
        games.append({"id": gid, "good": good, "winner": winner, "win_reason": s["win_reason"]})
    n = len(games)
    a_wins = won["a"]["good"] + won["a"]["evil"]
    low, high = wilson(a_wins, n)
    if not n:
        verdict = "no finished games yet"
    elif low > 0.5:
        verdict = f"A ({rec['a']['name']}) beats B more often than not"
    elif high < 0.5:
        verdict = f"B ({rec['b']['name']}) beats A more often than not"
    else:
        verdict = "not separated yet (the interval includes 50%), so play more games"
    return {"tag": tag, "model": rec["model"], "seed": rec["seed"], "mock": rec["mock"], "a": rec["a"], "b": rec["b"],
            "games": n, "stopped_games": stopped, "won": won, "played": played, "a_wins": a_wins, "b_wins": n - a_wins,
            "a_share": round(a_wins / n, 3) if n else None, "interval": [low, high], "verdict": verdict,
            "game_list": games}


def format_versus(v: dict) -> str:
    def cell(k, side):
        w, p = v["won"][k][side], v["played"][k][side]
        return f"{w} of {p}" + (f" ({w / p:.0%})" if p else "")
    lines = [f"{v['tag']}: {v['games']} finished game(s)" + (f", {v['stopped_games']} stopped" if v["stopped_games"] else "")
             + f", model {v['model']}" + (" (stand-in)" if v["mock"] else ""),
             f"{'':3}{'prompt':24} {'as Good':>14} {'as Evil':>14} {'total':>14}"]
    for k in "ab":
        total = f"{v['won'][k]['good'] + v['won'][k]['evil']} of {v['games']}"
        lines.append(f"{k.upper():3}{v[k]['name'][:24]:24} {cell(k, 'good'):>14} {cell(k, 'evil'):>14} {total:>14}")
    if v["games"]:
        lo, hi = v["interval"]
        lines.append(f"A wins {v['a_share']:.0%} of games (95% interval {lo:.0%} to {hi:.0%}): {v['verdict']}.")
    else:
        lines.append(v["verdict"] + ".")
    return "\n".join(lines)
