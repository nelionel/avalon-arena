"""Turn submission rules shared by the in-jail Stop hook and the referee.

Stdlib only: this file is executed inside every player's sandbox, where only
/opt/avalon/hooks (and the Python runtime) are readable.
"""

from __future__ import annotations

import json
import os
import re

WORD_LIMIT = 300
MIN_NOTE_WORDS = 20
KINDS = ("onboard", "propose", "discuss", "vote", "quest", "evil_huddle", "assassinate", "reflect")
BELIEF_KINDS = ("propose", "discuss", "vote", "quest", "evil_huddle", "assassinate")
ROLE_WORDS = ("merlin", "percival", "servant", "loyal", "morgana", "assassin", "mordred", "oberon", "minion", "good", "evil")


def count_words(text) -> int:
    return len(re.findall(r"\S+", text or ""))


def norm_player(p, players):
    """Map 'p3' / 'P3' / 3 to the canonical id 'P3' (or None if unknown)."""
    if isinstance(p, int):
        p = f"P{p}"
    if not isinstance(p, str):
        return None
    p = p.strip().upper()
    if re.fullmatch(r"\d+", p):
        p = "P" + p
    return p if p in players else None


def _msg_errors(sub, key, required, errs):
    if key not in sub:
        if required:
            errs.append(f'missing "{key}"')
        return
    msg = sub[key]
    if msg is None:
        if required:
            errs.append(f'"{key}" must be a string')
        return
    if not isinstance(msg, str):
        errs.append(f'"{key}" must be a string or null')
        return
    n = count_words(msg)
    if n > WORD_LIMIT:
        errs.append(f'"{key}" has {n} words; the limit is {WORD_LIMIT}. Shorten it.')


def _team_errors(team, spec, errs, key="team"):
    players = spec["players"]
    if not isinstance(team, list):
        errs.append(f'"{key}" must be a list of player ids like ["P1","P3"]')
        return None
    normed = [norm_player(p, players) for p in team]
    if any(p is None for p in normed):
        errs.append(f'"{key}" contains unknown player ids: {team}')
        return None
    if len(set(normed)) != len(normed):
        errs.append(f'"{key}" contains duplicates')
        return None
    if len(normed) != spec["team_size"]:
        errs.append(f'"{key}" must have exactly {spec["team_size"]} players (got {len(normed)})')
        return None
    return sorted(normed, key=lambda x: int(x[1:]))


def _belief_errors(sub, spec, errs, out):
    """Private beliefs: P(evil) for every other player, plus optional role guesses."""
    others = [p for p in spec.get("players", []) if p != spec.get("you")]
    b = sub.get("beliefs")
    if not isinstance(b, dict):
        errs.append('missing "beliefs": an object giving, for every other player, your probability (0 to 1) '
                    'that they are Evil, e.g. {"P1": 0.2, "P4": 0.7}')
        return
    normed = {}
    for k, v in b.items():
        p = norm_player(k, spec.get("players", []))
        if p is None or p == spec.get("you"):
            continue
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= float(v) <= 1:
            errs.append(f'"beliefs" value for {k} must be a number between 0 and 1')
            return
        normed[p] = round(float(v), 3)
    missing = [p for p in others if p not in normed]
    if missing:
        errs.append(f'"beliefs" is missing {", ".join(missing)}: give a probability for every other player')
        return
    out["beliefs"] = normed
    g = sub.get("role_guesses")
    if isinstance(g, dict):
        out["role_guesses"] = {p: str(v)[:40] for k, v in g.items()
                               if (p := norm_player(k, spec.get("players", []))) and p != spec.get("you")}


def validate_submission(spec: dict, sub) -> tuple[list[str], dict | None]:
    """Return (errors, normalized_submission)."""
    errs: list[str] = []
    if not isinstance(sub, dict):
        return ["submission must be a JSON object"], None
    if sub.get("turn") != spec["turn"]:
        errs.append(f'"turn" must be {spec["turn"]}')
    kind = spec["kind"]
    out: dict = {"turn": spec["turn"]}
    players = spec.get("players", [])

    if kind == "onboard":
        if sub.get("ack") is not True:
            errs.append('"ack" must be true')
        out["ack"] = True
    elif kind == "propose":
        team = _team_errors(sub.get("team"), spec, errs)
        _msg_errors(sub, "message", False, errs)
        out.update(team=team, message=sub.get("message"))
    elif kind == "discuss":
        _msg_errors(sub, "message", False, errs)
        if not isinstance(sub.get("ready_to_vote"), bool):
            errs.append('"ready_to_vote" must be true or false')
        out.update(message=sub.get("message") or None, ready_to_vote=bool(sub.get("ready_to_vote")))
        if sub.get("team") is not None:
            if not spec.get("can_amend"):
                errs.append('only the leader may include "team" (an amended proposal); remove it')
            else:
                out["team"] = _team_errors(sub.get("team"), spec, errs)
    elif kind == "vote":
        v = sub.get("vote")
        if v not in ("approve", "reject"):
            errs.append('"vote" must be "approve" or "reject"')
        out["vote"] = v
    elif kind == "quest":
        c = sub.get("card")
        if c not in spec["options"]:
            errs.append(f'"card" must be one of {spec["options"]}')
        out["card"] = c
    elif kind == "evil_huddle":
        _msg_errors(sub, "message", False, errs)
        t = sub.get("suggested_target")
        if t is not None and norm_player(t, players) not in spec["candidates"]:
            errs.append(f'"suggested_target" must be one of {spec["candidates"]} or null')
        out.update(message=sub.get("message") or None,
                   suggested_target=norm_player(t, players) if t is not None else None)
    elif kind == "assassinate":
        t = norm_player(sub.get("target"), players)
        if t not in spec["candidates"]:
            errs.append(f'"target" must be one of {spec["candidates"]}')
        _msg_errors(sub, "message", False, errs)
        out.update(target=t, message=sub.get("message"))
    elif kind == "reflect":
        _msg_errors(sub, "message", False, errs)
        out["message"] = sub.get("message")
    else:
        errs.append(f"unknown turn kind {kind}")
    if kind in BELIEF_KINDS:
        _belief_errors(sub, spec, errs, out)
    return errs, (out if not errs else None)


def check_turn_files(spec: dict) -> tuple[list[str], dict | None, int]:
    """Check the two required outputs of a turn on disk.

    Returns (errors, normalized_submission, note_words).
    """
    errs: list[str] = []
    note_words = 0
    notes = spec["notes_path"]
    if not os.path.isfile(notes):
        errs.append(f"private decision document missing: write {notes} explaining your reasoning")
    else:
        with open(notes, encoding="utf-8", errors="replace") as f:
            note_words = count_words(f.read())
        if note_words < MIN_NOTE_WORDS:
            errs.append(f"private decision document {notes} is too short ({note_words} words); "
                        f"explain your reasoning in at least {MIN_NOTE_WORDS} words")
    mem = spec.get("memory_path")
    if mem and spec.get("opened_at"):
        try:
            if os.path.getmtime(mem) < spec["opened_at"] - 1:
                errs.append(f"update your memory file {mem} with what happened and your current reads")
            with open(mem, encoding="utf-8", errors="replace") as f:
                if not f.read().strip():
                    errs.append(f"memory file {mem} is empty: record the current facts, beliefs and plan")
        except OSError:
            errs.append(f"create your memory file {mem}: facts, vote and quest record, claims, beliefs, plan")
    sub_path = spec["submit_path"]
    norm = None
    if not os.path.isfile(sub_path):
        errs.append(f"submission missing: write {sub_path}")
    else:
        try:
            with open(sub_path, encoding="utf-8") as f:
                sub = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            errs.append(f"submission {sub_path} is not valid JSON ({e})")
        else:
            sub_errs, norm = validate_submission(spec, sub)
            errs.extend(f"submission: {e}" for e in sub_errs)
    return errs, norm, note_words
