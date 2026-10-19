"""Collect everything the dashboard shows about a game into one JSON bundle."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from ..audit import _jsonl, audit_game, find_transcript, parse_transcript
from ..roles import ROLES, knowledge

ROOT = Path(__file__).resolve().parents[2]
GAMES = ROOT / "games"
_MEMORY_CACHE = {}


def _read(path: Path, default=None):
    try:
        return path.read_text(errors="replace")
    except OSError:
        return default


def _json(path: Path, default=None):
    t = _read(path)
    if t is None:
        return default
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return default


def game_dir(gid: str) -> Path:
    d = (GAMES / gid).resolve()
    if d.parent != GAMES.resolve() or not (d / "referee").is_dir():
        raise FileNotFoundError(gid)
    return d


def lineup(d: Path) -> dict:
    """Which model or bot plays each seat (public in an arena, like a player's name)."""
    seats = _json(d / "referee" / "seats.json", {}) or {}
    return {p: s["bot"] for p, s in sorted(seats.items(), key=lambda x: int(x[0][1:])) if s.get("bot")}


def model_colors(d: Path | None = None) -> dict:
    """Chip colors by label: models from models.toml, plus the teams seated in game d (tournaments)."""
    try:
        from ..models import load_models
        colors = {m.label: m.color for m in load_models().values()}
    except Exception:
        colors = {}
    seats = _json(d / "referee" / "seats.json", {}) if d else {}
    return {**colors, **{s["team"]["name"]: s["team"]["color"] for s in (seats or {}).values() if s.get("team")}}


def list_games() -> list[dict]:
    out = []
    for d in sorted(GAMES.iterdir(), reverse=True):
        st = _json(d / "referee" / "status.json")
        if not st:
            continue
        cfg = st.get("config") or {}
        out.append({"id": d.name, "status": st.get("status"), "phase": st.get("phase"), "turn": st.get("turn"),
                    "players": len(st.get("players") or []), "driver": st.get("driver"), "model": st.get("model"),
                    "mock": st.get("mock"), "winner": st.get("winner"), "win_reason": st.get("win_reason"),
                    "created_at": st.get("created_at"), "tag": cfg.get("tag"),
                    "adversarial": cfg.get("mock_adversarial"), "team_model": cfg.get("team_model"),
                    "models": list(dict.fromkeys(lineup(d).values()))})
    return out


def seat_notes(d: Path, p: str) -> dict:
    notes = {}
    nd = d / "players" / p.lower() / "work" / "notes"
    if nd.is_dir():
        for f in sorted(nd.glob("*.md")):
            key = f.stem.replace("turn_", "")
            notes[str(int(key)) if key.isdigit() else key] = _read(f, "")[:20000]
    return notes


def memory_history(d: Path, players: list[str], turns: list[dict]) -> dict:
    """Committed snapshots only, so rewinding never shows a player's later knowledge."""
    key = (str(d), len(turns), turns[-1]["turn"] if turns else 0)
    cached = _MEMORY_CACHE.get(key)
    if cached is not None:
        return cached
    out = {p: {} for p in players}
    wanted = {(t["turn"], p) for t in turns for p in t["seats"]}
    for turn, p in wanted:
        receipt = _json(d / "referee" / "pending" / f"{turn:04d}" / f"{p}.json", {})
        if "memory" in receipt:
            out[p][str(turn)] = receipt["memory"]
    for p in players:
        missing = {t for t, seat in wanted if seat == p and str(t) not in out[p]}
        gitdir = d / "referee" / "audit" / f"{p.lower()}.git"
        if not missing or not gitdir.exists():
            continue
        git = ["git", f"--git-dir={gitdir}"]
        log = subprocess.run(git + ["log", "--format=%H%x09%s"], capture_output=True, text=True)
        for line in log.stdout.splitlines():
            commit, _, subject = line.partition("\t")
            if not subject.startswith("turn ") or not subject[5:].isdigit():
                continue
            turn = int(subject[5:])
            if turn not in missing:
                continue
            r = subprocess.run(git + ["show", f"{commit}:memory.md"], capture_output=True, text=True)
            if r.returncode == 0:
                out[p][str(turn)] = r.stdout
                missing.remove(turn)
    # Bound a long-lived dashboard's cache without deleting any on-disk evidence.
    if len(_MEMORY_CACHE) > 20:
        _MEMORY_CACHE.clear()
    _MEMORY_CACHE[key] = out
    return out


def hooks_compact(d: Path, p: str, limit: int = 400) -> list[dict]:
    rows = _jsonl(d / "players" / p.lower() / "audit" / "hooks.jsonl")
    keep = []
    for r in rows[-limit:]:
        e = r.get("event")
        item = {"ts": r.get("ts"), "event": e}
        for k in ("tool", "guard", "turn", "status", "valid", "attempt", "errors", "prompt", "source", "message"):
            if r.get(k) is not None:
                item[k] = r[k]
        if e == "PreToolUse":
            item["input"] = r.get("input", "")[:240]
        keep.append(item)
    return keep


def public_commits(d: Path) -> list[dict]:
    out = subprocess.run(["git", "-C", str(d / "public"), "log", "--format=%h%x09%ct%x09%s"],
                         capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        h, ts, msg = (line.split("\t") + ["", ""])[:3]
        rows.append({"hash": h, "ts": int(ts) if ts.isdigit() else None, "msg": msg})
    return rows


def transcript_items(d: Path, p: str, max_text: int = 6000) -> list[dict]:
    seats = _json(d / "referee" / "seats.json", {})
    sid = (seats.get(p) or {}).get("session_id")
    if not sid:
        return []
    items = parse_transcript(find_transcript(d / "players" / p.lower(), sid))
    for it in items:
        for k in ("text", "result"):
            if isinstance(it.get(k), str) and len(it[k]) > max_text:
                it[k] = it[k][:max_text] + f"\n...[{len(it[k]) - max_text} more chars]"
        if it.get("kind") == "tool":
            it["input"] = {k: (v[:max_text] if isinstance(v, str) else v) for k, v in (it.get("input") or {}).items()}
    return items


def _epoch(ts) -> float | None:
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def thoughts_by_turn(d: Path, players: list[str], turns: list[dict], limit: int = 1600) -> dict:
    """Each seat's reasoning (thinking blocks + narration) grouped by the turn it happened in."""
    windows = [(t["turn"], t["opened_at"] - 1, t["closed_at"] + 1) for t in turns]
    out = {}
    for p in players:
        per = {}
        for it in transcript_items(d, p, max_text=limit):
            if it["kind"] not in ("think", "say"):
                continue
            ts = _epoch(it.get("ts"))
            turn = next((n for n, a, b in windows if ts and a <= ts <= b), None)
            if turn is None:
                continue
            per.setdefault(str(turn), []).append({"kind": it["kind"], "text": it["text"]})
        out[p] = {k: v[-4:] for k, v in per.items()}
    return out


def audio_index(d: Path) -> dict | None:
    return _json(d / "audio" / "index.json")


def cached_audit(d: Path) -> dict | None:
    rep_path = d / "referee" / "audit_report.json"
    st = _json(d / "referee" / "status.json", {})
    if rep_path.exists() and st.get("status") in ("finished", "error"):
        return _json(rep_path)
    try:
        return audit_game(d)
    except Exception as e:  # partial games (still setting up) may lack files
        return {"error": repr(e), "checks": [], "seats": {}}


def bundle(gid: str, *, include_transcripts: bool = False, public_only: bool = False) -> dict:
    d = game_dir(gid)
    ref = d / "referee"
    status = _json(ref / "status.json", {})
    roles = _json(ref / "roles.json", {})
    players = sorted(roles, key=lambda x: int(x[1:]))
    over = status.get("status") == "finished"
    b = {
        "generated_at": time.time(),
        "id": gid,
        "status": status,
        "summary": _json(ref / "summary.json"),
        "info": _json(d / "public" / "game.json", {}),
        "players": players,
        "turns": _jsonl(ref / "turns.jsonl"),
        "public_events": _jsonl(d / "public" / "events.jsonl"),
        "commits": public_commits(d),
        "rules": _read(d / "public" / "RULES.md", ""),
        "public_only": public_only,
        "lineup": lineup(d),
        "model_colors": model_colors(d),
    }
    b["audio"] = audio_index(d)
    if public_only:
        # Actor lists and completion badges during the Evil huddle reveal roles.
        # Configuration can also contain a deterministic role seed or credential paths.
        visible_status = {"game_id", "status", "driver", "model", "mock", "players", "phase", "turn",
                          "created_at", "updated_at", "winner", "win_reason", "score", "finished_at"}
        b["status"] = {k: v for k, v in status.items() if k in visible_status}
        b["roles"] = {p: roles[p] for p in players} if over else {}
        b["events"] = b["public_events"]
        b["turns"] = [t for t in b["turns"] if t["kind"] != "evil_huddle"]
        for t in b["turns"]:
            for p, original in list(t["seats"].items()):
                s = {k: v for k, v in original.items() if k in ("status", "defaulted", "submission")}
                t["seats"][p] = s
                if isinstance(s.get("submission"), dict):
                    s["submission"] = {k: v for k, v in s["submission"].items() if k not in ("beliefs", "role_guesses")}
                if t["kind"] in ("quest", "evil_huddle", "assassinate"):
                    s["submission"] = None
            t["actors"] = list(t["seats"])
        return b
    b.update({
        "roles": roles,
        "role_names": {p: ROLES[r].name for p, r in roles.items()},
        "sides": {p: ROLES[r].side for p, r in roles.items()},
        "knowledge": {p: knowledge(p, roles) for p in players},
        "role_cards": {p: _read(d / "players" / p.lower() / "work" / "ROLE.md", "") for p in players},
        "events": _jsonl(ref / "events.jsonl"),
        "notes": {p: seat_notes(d, p) for p in players},
        "memory_history": memory_history(d, players, b["turns"]),
        "journal": {p: _read(d / "players" / p.lower() / "work" / "journal.md") for p in players},
        "seats": _json(ref / "seats.json", {}),
        "procs": _json(ref / "procs.json", {}),
        "screens": {p: _read(ref / "screens" / f"{p.lower()}.ansi") for p in players},
        "hooks": {p: hooks_compact(d, p) for p in players},
        "audit": cached_audit(d),
        "thoughts": thoughts_by_turn(d, players, b["turns"]),
        "audio": audio_index(d),
        "referee_log": _jsonl(ref / "referee.jsonl")[-300:],
    })
    if include_transcripts:
        b["transcripts"] = {p: transcript_items(d, p) for p in players}
    if status.get("driver") == "codex":
        for p in players:
            session = _json(d / "players" / p.lower() / "audit" / "session.json", {})
            b["seats"].setdefault(p, {}).update(model_thread_id=session.get("thread_id"),
                                               persistent=session.get("ephemeral") is False)
    return b


def live(gid: str) -> dict:
    d = game_dir(gid)
    ref = d / "referee"
    roles = _json(ref / "roles.json", {})
    players = sorted(roles, key=lambda x: int(x[1:]))
    return {"status": _json(ref / "status.json", {}),
            "screens": {p: _read(ref / "screens" / f"{p.lower()}.ansi") for p in players},
            "procs": _json(ref / "procs.json", {}),
            "hooks": {p: hooks_compact(d, p, 60) for p in players},
            "turns_count": len(_jsonl(ref / "turns.jsonl")),
            "audio": audio_index(d)}
