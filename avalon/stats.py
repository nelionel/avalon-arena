"""Aggregate statistics over finished games."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from .audit import _jsonl
from .roles import EVIL, ROLES

ROOT = Path(__file__).resolve().parent.parent
GAMES = ROOT / "games"


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


KINDS = {"claude": "Claude Code (real model)", "codex": "Codex (real model)", "claude-mock": "Claude Code (mock model)", "scripted": "Scripted bots", "bots": "Local bots"}


def game_kind(s: dict) -> str:
    if s.get("driver") == "codex":
        return "codex"
    if s.get("driver") in ("scripted", "bots"):
        return s["driver"]
    return "claude-mock" if s.get("mock") else "claude"


def stats_by_kind(tag: str | None = None, game_id: str | None = None) -> dict:
    out = {}
    for k in KINDS:
        st = compute_stats(tag=tag, kind=k, game_id=game_id)
        if st["games"]:
            out[k] = {**st, "label": KINDS[k]}
    return out


def _disc(b: dict, side: dict):
    ev = [v for q, v in b.items() if side.get(q) == EVIL]
    gd = [v for q, v in b.items() if side.get(q) == "good"]
    if not ev or not gd:
        return None
    return sum(ev) / len(ev) - sum(gd) / len(gd)


def compute_stats(tag: str | None = None, driver: str | None = None, include_mock: bool = False,
                  model: str | None = None, kind: str | None = None, game_id: str | None = None) -> dict:
    rows = []
    for d in sorted(GAMES.iterdir()) if GAMES.exists() else []:
        if game_id and d.name != game_id:
            continue
        s = _load(d / "referee" / "summary.json")
        st = _load(d / "referee" / "status.json") or {}
        if not s or st.get("status") != "finished":
            continue
        if tag and s.get("tag") != tag:
            continue
        if driver and s.get("driver") != driver:
            continue
        if model and s.get("model") != model:
            continue
        if kind:
            if game_kind(s) != kind:
                continue
        elif s.get("mock") and not include_mock:
            continue
        rows.append((d, s))

    n = len(rows)
    out: dict = {"filters": {"tag": tag, "driver": driver, "model": model, "include_mock": include_mock, "kind": kind},
                 "games": n}
    if not n:
        return out
    winners = Counter(s["winner"] for _, s in rows)
    reasons = Counter(s["win_reason"] for _, s in rows)
    by_size: dict = defaultdict(lambda: {"games": 0, "good_wins": 0})
    role_stats: dict = defaultdict(lambda: {"games": 0, "wins": 0})
    assassin = {"attempts": 0, "hits": 0}
    proposals = approved = 0
    rounds = []
    msg_words = []
    votes = {"good_on_evil_team": Counter(), "good_on_clean_team": Counter(), "evil": Counter()}
    fail_plays = {"evil_on_quest": 0, "evil_failed": 0}
    disc_by_role: dict = defaultdict(list)          # final discernment per seat, by role
    good_final: list = []
    curve: dict = defaultdict(list)                 # good players' discernment after quest k
    claims: dict = defaultdict(lambda: Counter())   # "claims Merlin" -> {good: n, evil: n}
    claim_re = {"claims Merlin": re.compile(r"\bI(?:'m| am)\s+(?:the\s+)?Merlin\b", re.I),
                "claims Percival": re.compile(r"\bI(?:'m| am)\s+(?:the\s+)?Percival\b", re.I),
                "claims Good / loyal": re.compile(r"\bI(?:'m| am)\s+(?:a\s+)?(?:good|loyal)\b", re.I)}
    compliance = Counter()
    durations = defaultdict(list)
    games = []
    for d, s in rows:
        roles = s["roles"]
        side = {p: ROLES[r].side for p, r in roles.items()}
        by_size[s["players"]]["games"] += 1
        by_size[s["players"]]["good_wins"] += s["winner"] == "good"
        for p, r in roles.items():
            role_stats[r]["games"] += 1
            role_stats[r]["wins"] += side[p] == s["winner"]
        if s.get("assassin_target"):
            assassin["attempts"] += 1
            assassin["hits"] += s["win_reason"] == "merlin_assassinated"
        for pr in s["proposals"]:
            if pr["approved"] is None:
                continue
            proposals += 1
            approved += bool(pr["approved"])
            rounds.append(pr["rounds"])
            has_evil = any(side[p] == EVIL for p in pr["team"])
            for p, v in pr["votes"].items():
                key = "evil" if side[p] == EVIL else ("good_on_evil_team" if has_evil else "good_on_clean_team")
                votes[key][v] += 1
        for e in _jsonl(d / "referee" / "events.jsonl"):
            if e.get("type") == "quest_cards":
                for p, c in e["cards"].items():
                    if side[p] == EVIL:
                        fail_plays["evil_on_quest"] += 1
                        fail_plays["evil_failed"] += c == "fail"
            for x in e.get("entries", []) or []:
                if e.get("type") == "discussion" and x.get("message"):
                    msg_words.append(len(re.findall(r"\S+", x["message"])))
        turns = _jsonl(d / "referee" / "turns.jsonl")
        # beliefs: discernment = mean P(evil | evil) - mean P(evil | good), per report
        last_b: dict = {}
        quest_done = 0
        for t in turns:
            for p, seat in t["seats"].items():
                b = (seat.get("submission") or {}).get("beliefs")
                if b:
                    last_b[p] = b
            if t["kind"] == "quest":
                quest_done += 1
                for p, b in last_b.items():
                    if side[p] == "good":
                        dsc = _disc(b, side)
                        if dsc is not None:
                            curve[quest_done].append(dsc)
        for p, b in last_b.items():
            dsc = _disc(b, side)
            if dsc is not None:
                disc_by_role[roles[p]].append(dsc)
                if side[p] == "good":
                    good_final.append(dsc)
        for e in _jsonl(d / "public" / "events.jsonl"):
            texts = [(e.get("leader") or e.get("assassin"), e.get("message"))] + \
                    [(x.get("player"), x.get("message")) for x in e.get("entries", []) or []]
            for who, msg in texts:
                if who and msg:
                    for label, rx in claim_re.items():
                        if rx.search(msg):
                            claims[label][side[who]] += 1
        for t in turns:
            durations[t["kind"]].append(t["closed_at"] - t["opened_at"])
            for seat in t["seats"].values():
                compliance["submissions"] += 1
                compliance["defaulted"] += bool(seat.get("defaulted"))
                compliance[seat.get("status", "?")] += 1
                compliance["stop_blocks"] += seat.get("stop_blocks", 0) or 0
                compliance["guard_denials"] += seat.get("guard_denials", 0) or 0
        games.append({"id": d.name, "winner": s["winner"], "reason": s["win_reason"], "players": s["players"],
                      "turns": s["turns"], "score": s["score"], "tag": s.get("tag"), "model": s.get("model"),
                      "driver": s.get("driver"), "mock": s.get("mock"),
                      "minutes": round(((s.get("finished_at") or 0) - (s.get("started_at") or 0)) / 60, 1)})

    def rate(c: Counter):
        tot = sum(c.values())
        return {"approve": c["approve"], "reject": c["reject"],
                "approve_rate": round(c["approve"] / tot, 3) if tot else None}

    out.update({
        "winners": dict(winners),
        "good_win_rate": round(winners["good"] / n, 3),
        "reasons": dict(reasons),
        "by_player_count": {k: v for k, v in sorted(by_size.items())},
        "roles": {r: {**v, "win_rate": round(v["wins"] / v["games"], 3),
                      "discernment": round(sum(disc_by_role[r]) / len(disc_by_role[r]), 3) if disc_by_role[r] else None}
                  for r, v in sorted(role_stats.items())},
        "beliefs": {"good_final_discernment": round(sum(good_final) / len(good_final), 3) if good_final else None,
                    "reports": sum(len(v) for v in disc_by_role.values())},
        "learning_curve": {str(k): round(sum(v) / len(v), 3) for k, v in sorted(curve.items()) if v},
        "claims": {k: dict(v) for k, v in claims.items()},
        "assassination": {**assassin, "hit_rate": round(assassin["hits"] / assassin["attempts"], 3)
                          if assassin["attempts"] else None},
        "proposals": {"total": proposals, "approved": approved,
                      "approval_rate": round(approved / proposals, 3) if proposals else None,
                      "avg_discussion_rounds": round(sum(rounds) / len(rounds), 2) if rounds else None,
                      "rounds_histogram": dict(sorted(Counter(rounds).items()))},
        "votes": {k: rate(v) for k, v in votes.items()},
        "quest_cards": {**fail_plays, "evil_fail_rate": round(fail_plays["evil_failed"] / fail_plays["evil_on_quest"], 3)
                        if fail_plays["evil_on_quest"] else None},
        "messages": {"count": len(msg_words), "avg_words": round(sum(msg_words) / len(msg_words), 1) if msg_words else None,
                     "max_words": max(msg_words) if msg_words else None},
        "compliance": dict(compliance),
        "turn_seconds": {k: round(sum(v) / len(v), 1) for k, v in durations.items()},
        "list": games,
    })
    return out
