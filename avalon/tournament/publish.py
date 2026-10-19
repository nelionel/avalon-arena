"""Publishing a tournament: public bundles, a static site, and trajectories for teams to learn from.

What spectators may see:
  running game   the public record only: no roles, private notes, memory, beliefs, reasoning or prompts
  finished game  roles, notes, memory and reasoning too, but never machine internals (terminals, transcripts,
                 process facts, logs), and team prompts only once the reveal time has passed
"""

from __future__ import annotations

import html
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from ..dashboard import data
from ..roles import ROLES
from .series import compute_standings

INTERNAL = ("journal", "seats", "procs", "screens", "hooks", "audit", "referee_log", "transcripts")
VISIBLE_STATUS = ("game_id", "status", "driver", "model", "mock", "players", "phase", "turn", "created_at",
                  "updated_at", "winner", "win_reason", "score", "finished_at")


def reveal_due(when: str | None) -> bool:
    """Whether team prompts are public: when is "never" (default), "now", or an ISO date/time (UTC unless given)."""
    if not when or when == "never":
        return False
    if when == "now":
        return True
    at = datetime.fromisoformat(when)
    return datetime.now(timezone.utc) >= (at if at.tzinfo else at.replace(tzinfo=timezone.utc))


def public_games(tag: str | None = None) -> list[dict]:
    """The dashboard's game list, limited to tournament games (of one series when tag is given)."""
    return [{k: v for k, v in g.items() if k != "adversarial"} for g in data.list_games()
            if g.get("team_model") and (tag is None or g["tag"] == tag)]


def team_prompts(d: Path) -> dict:
    """{seat: team prompt} as played in this game (the snapshot each seat took at the start)."""
    out = {}
    for seat in sorted((d / "players").glob("p*")):
        snap = data._json(seat / "audit" / "prompt.json")
        if snap:
            out[seat.name.upper()] = snap["team_prompt"]
    return out


def public_bundle(gid: str, *, reveal_prompts: bool = False) -> dict:
    d = data.game_dir(gid)
    status = data._json(d / "referee" / "status.json", {}) or {}
    if status.get("status") != "finished":
        b = data.bundle(gid, public_only=True)
    else:
        b = data.bundle(gid)
        for k in INTERNAL:
            b.pop(k, None)
        b["status"] = {k: v for k, v in status.items() if k in VISIBLE_STATUS}
        if reveal_prompts:
            b["team_prompts"] = team_prompts(d)
    b["public_mode"] = True
    return b


def prompts_by_team(tag: str) -> dict:
    out = {}
    for g in public_games(tag):
        d = data.GAMES / g["id"]
        teams = (data._json(d / "referee" / "summary.json", {}) or {}).get("teams") or {}
        for p, text in team_prompts(d).items():
            if p in teams:
                out[teams[p]["id"]] = text
    return out


def public_standings(tag: str | None = None, *, reveal_prompts: bool = False) -> dict:
    st = compute_standings(tag)
    st["prompts_revealed"] = reveal_prompts
    if reveal_prompts and st["tag"]:
        st["team_prompts"] = prompts_by_team(st["tag"])
    return st


def export_site(tag: str | None, out_dir: str | Path, *, reveal_prompts: bool = False) -> dict:
    """A static site for GitHub Pages or Cloudflare Pages: standings (index.html) and replays (replay.html)."""
    from ..dashboard.server import SKELETON, page
    out = Path(out_dir)
    st = public_standings(tag, reveal_prompts=reveal_prompts)
    if not st["tag"]:
        raise ValueError("no tournament games found")
    games = public_games(st["tag"])
    if not games:
        raise ValueError(f"no games for tournament {st['tag']}")
    (out / "data").mkdir(parents=True, exist_ok=True)
    featured = next((g["id"] for g in games if g["status"] == "finished"), games[0]["id"])
    for g in games:
        b = public_bundle(g["id"], reveal_prompts=reveal_prompts)
        b["games_index"], b["standings"] = games, st
        if g["id"] == featured:
            (out / "replay.html").write_text(SKELETON.replace("{body}", page(b)))
        (out / "data" / f"{g['id']}.json").write_text(json.dumps(b, separators=(",", ":")))
    shutil.copytree(Path(data.__file__).parent / "static" / "chars", out / "chars", dirs_exist_ok=True)
    (out / "standings.json").write_text(json.dumps(st, indent=1))
    (out / "index.html").write_text(standings_page(st, games))
    return {"tag": st["tag"], "out": str(out), "games": len(games), "index": str(out / "index.html")}


def standings_page(st: dict, games: list[dict]) -> str:
    e = html.escape
    pct = lambda v: "–" if v is None else f"{round(v * 100)}%"
    num = lambda v, digits: "–" if v is None else f"{v:.{digits}f}"
    rows = "".join(
        f"<tr><td class=n>{t['rank']}</td><td><span class=dot style='background:{e(t['color'])}'></span>{e(t['name'])}</td>"
        f"<td class=n>{t['games']}</td><td class=n><b>{num(t['score'], 3)}</b></td>"
        f"<td class=n>{pct(t['good']['win_rate'])} <small>({t['good']['games']})</small></td>"
        f"<td class=n>{pct(t['evil']['win_rate'])} <small>({t['evil']['games']})</small></td>"
        f"<td class=n>{num(t['evil_read'], 2)}</td>"
        f"<td class=n>{pct(t['merlin']['survival'])} <small>({t['merlin']['games']})</small></td>"
        f"<td class=n>{pct(t['assassin']['accuracy'])} <small>({t['assassin']['shots']})</small></td>"
        f"<td class=n>{t['faults'] or ''}</td></tr>" for t in st["teams"])
    played = "".join(
        f"<li><a href='replay.html?game={e(g['id'])}'>{e(time.strftime('%Y-%m-%d %H:%M', time.gmtime(g['created_at'] or 0)))}</a> "
        f"· {e(' · '.join(g['models']))} · <b>{e(g['winner'] + ' won' if g['winner'] else g['status'] or '')}</b>"
        f"{' (' + e((g['win_reason'] or '').replace('_', ' ')) + ')' if g['winner'] else ''}</li>" for g in games)
    names = {t["id"]: t["name"] for t in st["teams"]}
    prompts = "".join(f"<details><summary>{e(names.get(tid, tid))}</summary><pre>{e(text)}</pre></details>"
                      for tid, text in sorted((st.get("team_prompts") or {}).items()))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Avalon Tournament</title>
<style>
:root {{ --bg:#f7f5f0; --card:#fff; --ink:#1d1b16; --muted:#6b665c; --line:#e3ded3; --accent:#8a6d2b; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#16140f; --card:#1f1c16; --ink:#efe9dc; --muted:#a39b8b; --line:#38332a; --accent:#d9b56a; }} }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif; }}
main {{ max-width:1080px; margin:0 auto; padding:24px 16px 48px; }}
h1 {{ margin:0 0 4px; font-size:26px; }} h2 {{ font-size:18px; margin:28px 0 8px; }}
.sub {{ color:var(--muted); margin:0 0 18px; }} a {{ color:var(--accent); }}
.wrap {{ overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:10px; }}
table {{ border-collapse:collapse; width:100%; min-width:720px; }}
th, td {{ padding:8px 10px; border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; }}
th {{ font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }}
.n {{ text-align:right; font-variant-numeric:tabular-nums; }} small {{ color:var(--muted); }}
.dot {{ display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:8px; }}
ul {{ padding-left:18px; }} li {{ margin:4px 0; }}
details {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:8px 12px; margin:6px 0; }}
pre {{ white-space:pre-wrap; overflow-wrap:anywhere; font-size:13px; }}
</style></head><body><main>
<h1>Avalon Tournament</h1>
<p class="sub">{e(st['tag'])} · {st['games']} finished game(s){f" · {st['stopped_games']} stopped" if st['stopped_games'] else ''} ·
updated {e(time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(st['generated_at'])))} · <a href="replay.html">watch the replays</a></p>
<div class="wrap"><table><thead><tr><th class=n>#</th><th>Team</th><th class=n>Games</th><th class=n>Score</th>
<th class=n>Good</th><th class=n>Evil</th><th class=n title="Good players other than Merlin: average final P(Evil) for real Evil players minus that for Good players">Evil read</th>
<th class=n>Merlin safe</th><th class=n>Assassin</th><th class=n>Faults</th></tr></thead><tbody>{rows}</tbody></table></div>
<p class="sub">Score is the mean of a team's Good and Evil win rates. Ties go to Evil read, then Merlin survival,
then Assassin accuracy, then fewer faults (moves the referee had to play for a team after invalid replies).</p>
<h2>Games</h2><ul>{played}</ul>
{f"<h2>Team prompts</h2>{prompts}" if prompts else ""}
</main></body></html>
"""


def trajectories(game_ids: list[str], *, team: str | None = None):
    """One record per seat-turn of the prompt players: input sections, memory before and after, move and usage."""
    for gid in game_ids:
        d = data.game_dir(gid)
        s = data._json(d / "referee" / "summary.json") or {}
        rows = []
        for seat in sorted((d / "players").glob("p*")):
            snap = data._json(seat / "audit" / "prompt.json")
            if not snap or (team and snap["team"]["id"] != team):
                continue
            side = ROLES[snap["role"]].side
            for rec in data._jsonl(seat / "audit" / "prompt-turns.jsonl"):
                rows.append({
                    "game": gid, "turn": rec["turn"], "kind": rec["kind"], "player": rec["player"],
                    "team": snap["team"]["id"], "team_name": snap["team"]["name"], "model": snap["model"],
                    "role": snap["role"], "side": side,
                    "input": {"harness": snap["harness"], "role_card": snap["role_card"],
                              "team_prompt": snap["team_prompt"], **rec["sections"]},
                    "memory_before": rec["memory_before"], "memory_after": rec["memory_after"], "move": rec["move"],
                    "reason": rec["reason"], "reasoning": rec["reasoning"], "usage": rec["usage"],
                    "attempts": rec["attempts"], "rejected": rec["rejected"], "fault": rec["fault"],
                    "outcome": {"winner": s["winner"], "win_reason": s["win_reason"], "won": s["winner"] == side}
                    if s.get("winner") else None})
        yield from sorted(rows, key=lambda r: (r["turn"], int(r["player"][1:])))
