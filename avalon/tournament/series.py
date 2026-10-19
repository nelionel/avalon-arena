"""The tournament series: balanced role rotation, the runner, and standings.

  bin/avalon tournament --teams teams/ --games 16 --model haiku-5.5-or --parallel 2 --seed 7
  bin/avalon standings --tag <tag>

The setup is fixed: 8 teams, one seat each, 5 Good (Merlin, Percival, 3 Loyal Servants) against
3 Evil (Morgana, the Assassin, Oberon). Roles follow a Latin square over each block of 8
games: in every block each team holds each of the 8 role slots exactly once, so over 16 games every team
plays every role twice (Good 10 times, Evil 6 times). Rows, columns, role slots and seat order are
shuffled from --seed, so the schedule is reproducible but not predictable from the outside.

Score = mean of a team's Good win rate and Evil win rate. Tie-breaks, in order: Evil read (higher),
Merlin survival (higher), Assassin accuracy (higher), faults (fewer).
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections import defaultdict
from pathlib import Path

from ..roles import ROLES
from .teams import Team

TEAMS = 8
TOURNAMENT_ROLES = ["merlin", "percival", "servant", "servant", "servant", "morgana", "assassin", "oberon"]


def schedule(team_ids: list[str], games: int, seed: int | None) -> list[dict]:
    """[{"seats": {"P1": team, ...}, "roles": {"P1": role, ...}}, ...]: one seat per team, roles balanced."""
    n = len(team_ids)
    if n != TEAMS:
        raise ValueError(f"a tournament needs exactly {TEAMS} teams (one seat each in an 8-player game); got {n}. "
                         "Fill empty places with baseline copies: bin/avalon teams demo DIR --count N")
    slots, rng, plans = TOURNAMENT_ROLES, random.Random(seed), []
    for g in range(games):
        k = g % n
        if k == 0:                       # a new block: a fresh shuffled Latin square
            rows, cols, syms = rng.sample(range(n), n), rng.sample(range(n), n), rng.sample(range(n), n)
        role_of = {t: slots[syms[(rows[i] + cols[k]) % n]] for i, t in enumerate(team_ids)}
        order = rng.sample(team_ids, n)
        seats = {f"P{i + 1}": t for i, t in enumerate(order)}
        plans.append({"seats": seats, "roles": {p: role_of[t] for p, t in seats.items()}})
    return plans


async def run_tournament(teams: list[Team], games: int, *, model: str, parallel: int = 1, seed: int | None = None,
                         tag: str | None = None, mock: bool = False, timeout: float = 600, max_rounds: int = 3,
                         reflect: bool = True) -> tuple[str, list[str]]:
    """Play the series; returns (tag, game ids). A game that stops is reported and the series goes on."""
    from ..drivers.local_bot import model_label
    from ..referee import GAMES, GameConfig, Referee
    model_label(f"model:{model}", mock)          # raises, naming what is missing, before any game starts
    seed = seed if seed is not None else random.SystemRandom().randrange(10 ** 9)
    tag = tag or f"tournament-{time.strftime('%Y%m%d-%H%M')}"
    by_id = {t.id: t for t in teams}
    plans = schedule(sorted(by_id), games, seed)
    GAMES.mkdir(parents=True, exist_ok=True)
    manifest = GAMES / f".{tag}.tournament.json"
    record = {"tag": tag, "model": model, "seed": seed, "mock": mock, "games": games,
              "teams": {t.id: t.meta() for t in teams}, "schedule": plans, "game_ids": [None] * games}
    manifest.write_text(json.dumps(record, indent=1))
    sem, done = asyncio.Semaphore(parallel), []

    async def one(i, plan):
        async with sem:
            cfg = GameConfig(players=TEAMS, roles=TOURNAMENT_ROLES, driver="bots", mock=mock, tag=tag, voice=False, seed=seed + i,
                             seats={p: f"team:{by_id[t].path}" for p, t in plan["seats"].items()},
                             assignment=plan["roles"], team_model=model, turn_timeout=timeout,
                             onboard_timeout=max(timeout, 420), max_rounds=max_rounds, reflect=reflect)
            r = Referee(cfg)
            record["game_ids"][i] = r.gid
            manifest.write_text(json.dumps(record, indent=1))
            print(f"game {i + 1}/{games} {r.gid}: " + ", ".join(f"{p} {by_id[t].name}" for p, t in plan["seats"].items()),
                  flush=True)
            try:
                await r.run()
                s = json.loads((r.ref / "summary.json").read_text())
                faults = sum(len(v) for v in (s.get("faults") or {}).values())
                print(f"game {i + 1}/{games} {r.gid}: {s['winner'].upper()} wins ({s['win_reason']})"
                      + (f", {faults} fault(s)" if faults else ""), flush=True)
            except Exception as e:      # keep the series going; the game keeps its saved state
                print(f"game {i + 1}/{games} {r.gid}: stopped: {str(e)[:400]}", flush=True)
            done.append(r.gid)

    await asyncio.gather(*(one(i, p) for i, p in enumerate(plans)))
    return tag, done


def _jsonl(path: Path) -> list[dict]:
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except OSError:
        return []


def tournament_tags(games_dir: Path | None = None) -> list[str]:
    """Tags of tournament series, newest first."""
    from ..referee import GAMES
    games_dir = games_dir or GAMES
    found = {}
    for d in games_dir.iterdir() if games_dir.exists() else []:
        try:
            st = json.loads((d / "referee" / "status.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        cfg = st.get("config") or {}
        if cfg.get("team_model") and cfg.get("tag"):
            found[cfg["tag"]] = max(found.get(cfg["tag"], 0), st.get("created_at") or 0)
    return sorted(found, key=lambda t: -found[t])


def compute_standings(tag: str | None = None, games_dir: Path | None = None, *, include_mock: bool = True) -> dict:
    """Standings by team over the finished games of one series (the newest series when tag is None)."""
    from ..referee import GAMES
    games_dir = games_dir or GAMES
    tag = tag or next(iter(tournament_tags(games_dir)), None)
    rows: dict = defaultdict(lambda: defaultdict(float))
    meta, counted, stopped, games = {}, 0, 0, []
    for d in sorted(games_dir.iterdir()) if games_dir.exists() and tag else []:
        try:
            st = json.loads((d / "referee" / "status.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        cfg = st.get("config") or {}
        if not cfg.get("team_model") or cfg.get("tag") != tag or (cfg.get("mock") and not include_mock):
            continue
        try:
            s = json.loads((d / "referee" / "summary.json").read_text())
        except (OSError, json.JSONDecodeError):
            s = None
        if st.get("status") != "finished" or not s:
            stopped += st.get("status") not in ("running", "setup", "starting", "starting_players")
            games.append({"id": d.name, "status": st.get("status"), "winner": None})
            continue
        counted += 1
        games.append({"id": d.name, "status": "finished", "winner": s["winner"], "win_reason": s["win_reason"],
                      "teams": {p: m["id"] for p, m in s["teams"].items()}})
        final = {}
        for t in _jsonl(d / "referee" / "turns.jsonl"):
            for p, seat in (t.get("seats") or {}).items():
                if (seat.get("submission") or {}).get("beliefs"):
                    final[p] = seat["submission"]["beliefs"]
        sides = {q: ROLES[r].side for q, r in s["roles"].items()}
        for p, m in s["teams"].items():
            meta[m["id"]] = m
            role, side = s["roles"][p], sides[p]
            won = s["winner"] == side
            r = rows[m["id"]]
            r["games"] += 1
            r[f"{side}_games"] += 1
            r[f"{side}_wins"] += won
            r[f"role_{role}"] += 1
            b = final.get(p)
            if side == "good" and role != "merlin" and b:      # Merlin already knows; reads count for the others
                ev = [v for q, v in b.items() if sides.get(q) == "evil"]
                gd = [v for q, v in b.items() if sides.get(q) == "good"]
                if ev and gd:
                    r["read_sum"] += sum(ev) / len(ev) - sum(gd) / len(gd)
                    r["read_n"] += 1
            if role == "merlin" and s.get("assassin_target"):
                r["merlin_games"] += 1
                r["merlin_survived"] += s["win_reason"] != "merlin_assassinated"
            if role == "assassin" and s.get("assassin_target"):
                r["assassin_shots"] += 1
                r["assassin_hits"] += s["win_reason"] == "merlin_assassinated"
            for f in (s.get("faults") or {}).get(p, []):
                r["faults" if f["cause"] == "invalid" else "errors"] += 1
            for u in _jsonl(d / "players" / p.lower() / "audit" / "usage.jsonl"):
                r["cost"] += u.get("cost") or 0
                r["cost_seen"] += "cost" in u
    teams = []
    for tid, r in rows.items():
        rate = lambda a, b: round(r[a] / r[b], 3) if r[b] else None
        good, evil = rate("good_wins", "good_games"), rate("evil_wins", "evil_games")
        sides = [x for x in (good, evil) if x is not None]
        teams.append({
            "id": tid, "name": meta[tid]["name"], "color": meta[tid]["color"], "games": int(r["games"]),
            "score": round(sum(sides) / len(sides), 3) if sides else None,
            "good": {"games": int(r["good_games"]), "wins": int(r["good_wins"]), "win_rate": good},
            "evil": {"games": int(r["evil_games"]), "wins": int(r["evil_wins"]), "win_rate": evil},
            "evil_read": round(r["read_sum"] / r["read_n"], 3) if r["read_n"] else None,
            "merlin": {"games": int(r["merlin_games"]), "survival": rate("merlin_survived", "merlin_games")},
            "assassin": {"shots": int(r["assassin_shots"]), "accuracy": rate("assassin_hits", "assassin_shots")},
            "faults": int(r["faults"]), "provider_errors": int(r["errors"]),
            "roles": {k[5:]: int(v) for k, v in sorted(r.items()) if k.startswith("role_")},
            "cost_per_game": round(r["cost"] / r["games"], 4) if r["cost_seen"] else None,
        })
    low = lambda v, floor: floor if v is None else v
    teams.sort(key=lambda t: (-low(t["score"], -1), -low(t["evil_read"], -2), -low(t["merlin"]["survival"], -1),
                              -low(t["assassin"]["accuracy"], -1), t["faults"], t["name"]))
    for i, t in enumerate(teams):
        t["rank"] = i + 1
    return {"tag": tag, "games": counted, "stopped_games": stopped, "teams": teams,
            "game_list": sorted(games, key=lambda g: g["id"]), "generated_at": time.time()}


def format_standings(st: dict) -> str:
    pct = lambda v: "  -  " if v is None else f"{v * 100:4.0f}%"
    num = lambda v: "-" if v is None else f"{v:.2f}"
    lines = [f"{st['tag'] or '(no tournament yet)'}: {st['games']} finished game(s)"
             + (f", {st['stopped_games']} stopped" if st["stopped_games"] else ""),
             f"{'#':>2} {'team':22} {'games':>5} {'score':>6} {'good':>11} {'evil':>11} {'read':>5} {'merlin':>7} "
             f"{'shot':>6} {'faults':>6} {'errors':>6} {'$/game':>7}"]
    for t in st["teams"]:
        g, e = t["good"], t["evil"]
        lines.append(f"{t['rank']:>2} {t['name'][:22]:22} {t['games']:>5} {num(t['score']):>6} "
                     f"{pct(g['win_rate']):>6} ({g['games']:>2}) {pct(e['win_rate']):>6} ({e['games']:>2}) "
                     f"{num(t['evil_read']):>5} {pct(t['merlin']['survival']):>7} {pct(t['assassin']['accuracy']):>6} "
                     f"{t['faults']:>6} {t['provider_errors']:>6} "
                     f"{'-' if t['cost_per_game'] is None else '%.3f' % t['cost_per_game']:>7}")
    lines.append("score = mean of Good and Evil win rates; ties: Evil read, Merlin survival, Assassin accuracy, fewest faults")
    return "\n".join(lines)
