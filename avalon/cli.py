"""avalon - run sandboxed Claude Code Avalon games, inspect and audit them.

  avalon run --players 7 --mock            dry run: real Claude Code TUIs, mock model
  avalon run --players 7                   real run with Haiku 5.5 (needs secrets/credentials.env)
  avalon run --players 7 --driver scripted heuristic bots, no Claude (seconds)
  avalon run --players 5 --seat P1=bots/example_bot.py   your bot vs scripted bots (no root)
  avalon run --players 6 --seat P1=model:haiku-5.5 --seat P2=model:qwen-3.8-flash-next   models at seats
  avalon arena --games 4                   Haiku 5.5 vs GPT-6 Luna vs Qwen3.8 Flash Next, then a leaderboard
  avalon leaderboard                       results by model / bot over finished arena and bot games
  avalon teams check teams/                check tournament submissions, then play them in a stand-in game
  avalon tournament --teams teams/ --games 16 --parallel 2    one seat per team, roles balanced (TOURNAMENT.md)
  avalon standings                         tournament standings by team
  avalon trajectories GAME_ID ...          one JSONL record per seat-turn of the prompt players
  avalon models                            which models are configured and ready on this machine
  avalon resume GAME_ID                    resume an interrupted local Codex game
  avalon batch --games 20 --parallel 2 ... many games, then `avalon stats`
  avalon dashboard                         live dashboard on http://127.0.0.1:8777
  avalon export GAME_ID --out game.html    single-file replay (for sharing as an artifact)
  avalon audit GAME_ID                     isolation / compliance report
  avalon stats [--tag T]                   aggregate results over finished games
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time

from .drivers.local_bot import resolve_spec
from .referee import GAMES, GameConfig, Referee


def add_game_args(ap):
    ap.add_argument("--players", type=int, default=7, help="5-10")
    ap.add_argument("--roles", help="comma-separated role keys, e.g. merlin,percival,servant,servant,morgana,assassin,oberon")
    ap.add_argument("--driver", choices=["claude", "codex", "scripted", "bots"], default=None,
                    help="default: bots when --seat/--fill is given, otherwise claude")
    ap.add_argument("--seat", action="append", default=[], metavar="P1=BOT",
                    help="bots driver: who plays a seat. BOT is a .py file (optionally FILE.py:ClassName), "
                         "'random' or 'scripted'. Repeat for more seats.")
    ap.add_argument("--fill", default=None, metavar="BOT",
                    help="bots driver: who plays the seats --seat does not name (default scripted)")
    ap.add_argument("--model", default=None, help="Defaults to gpt-6-luna for Codex, claude-haiku-5-5 for Claude")
    ap.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default="high",
                    help="players' reasoning effort (default high, for richer reasoning to inspect)")
    ap.add_argument("--mock", action="store_true", help="players talk to a local mock model (no API cost)")
    ap.add_argument("--adversarial", action="store_true", help="(with --mock) players try to break isolation")
    ap.add_argument("--mock-latency", type=float, default=0.3)
    ap.add_argument("--max-rounds", type=int, default=10, help="discussion rounds per proposal (max 10)")
    ap.add_argument("--huddle-rounds", type=int, default=1)
    ap.add_argument("--no-reflect", action="store_true")
    ap.add_argument("--timeout", type=float, default=300, help="seconds per turn")
    ap.add_argument("--no-bash", action="store_true", help="remove the read-only Bash tool")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--tag")
    ap.add_argument("--credentials", default=None)
    ap.add_argument("--max-turns", type=int, help="stop after N turns (tests)")
    ap.add_argument("--no-voice", action="store_true", help="do not render messages to speech")
    ap.add_argument("--no-hook-guard", action="store_true",
                    help="TEST ONLY: disable the hook path guard to show the deeper layers hold on their own")


def driver_of(a) -> str:
    bots = bool(a.seat or a.fill)
    if bots and a.driver not in (None, "bots", "scripted"):
        sys.exit(f"--seat and --fill choose local bots; they cannot be combined with --driver {a.driver}")
    return "bots" if bots else a.driver or "claude"


def parse_seats(items: list[str]) -> dict[str, str]:
    base = os.environ.get("AVALON_CWD") or os.getcwd()   # bin/avalon changes directory; resolve paths as typed
    seats = {}
    for item in items:
        p, sep, spec = item.partition("=")
        p = p.strip().upper()
        p = p if p.startswith("P") else "P" + p
        if not sep or not spec or not p[1:].isdigit():
            sys.exit(f"--seat {item!r}: expected SEAT=BOT, e.g. P1=bots/example_bot.py")
        if p in seats:
            sys.exit(f"--seat names {p} twice")
        seats[p] = resolve_spec(spec.strip(), base)
    return seats


def cfg_from(a) -> GameConfig:
    driver = driver_of(a)
    cfg = GameConfig(players=a.players, roles=a.roles.split(",") if a.roles else None, driver=driver,
                     model=a.model,
                     effort=a.effort, mock=a.mock, mock_adversarial=a.adversarial,
                     mock_latency=a.mock_latency, max_rounds=min(10, a.max_rounds), huddle_rounds=a.huddle_rounds,
                     reflect=not a.no_reflect, turn_timeout=a.timeout, allow_bash=not a.no_bash,
                     seed=a.seed, tag=a.tag, max_turns=a.max_turns, hook_guard=not a.no_hook_guard,
                     voice=not a.no_voice and driver in ("claude", "codex"),
                     seats=parse_seats(a.seat) or None,
                     fill=resolve_spec(a.fill, os.environ.get("AVALON_CWD")) if a.fill else "scripted")
    if a.credentials:
        cfg.credentials_file = a.credentials
    return cfg


async def run_one(cfg: GameConfig | None = None, *, resume_game=None, max_turns=None) -> str:
    ref = Referee.from_saved(resume_game, max_turns=max_turns) if resume_game else Referee(cfg)
    print(f"game {ref.gid} -> {ref.dir}", flush=True)
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        loop.add_signal_handler(sig, task.cancel)   # cancellation runs the referee's teardown
    await ref.run()
    if not (ref.ref / "summary.json").exists():
        print(f"game {ref.gid}: stopped after {ref.turn_no} turns", flush=True)
        return ref.gid
    s = json.loads((ref.ref / "summary.json").read_text())
    print(f"game {ref.gid}: {s['winner'].upper()} wins ({s['win_reason']}) after {s['turns']} turns", flush=True)
    return ref.gid


async def run_batch(a):
    sem = asyncio.Semaphore(a.parallel)
    done = []

    async def one(i):
        async with sem:
            cfg = cfg_from(a)
            cfg.tag = a.tag or f"batch-{time.strftime('%Y%m%d-%H%M')}"
            if a.seed is not None:
                cfg.seed = a.seed + i
            try:
                done.append(await run_one(cfg))
            except Exception as e:  # keep the batch going
                print(f"game {i} failed: {e!r}", flush=True)

    await asyncio.gather(*(one(i) for i in range(a.games)))
    print(f"{len(done)}/{a.games} games finished")


def cleanup():
    """Kill the tmux server (and so every player) of games whose referee died."""
    for g in sorted(GAMES.iterdir()):
        st_path = g / "referee" / "status.json"
        sock = g / "referee" / "tmux.sock"
        if not st_path.exists() or not sock.exists():
            continue
        st = json.loads(st_path.read_text())
        pid = st.get("referee_pid")
        alive = bool(pid) and os.path.exists(f"/proc/{pid}") and b"avalon" in open(f"/proc/{pid}/cmdline", "rb").read()
        if alive:
            continue
        r = subprocess.run(["tmux", "-S", str(sock), "kill-server"], capture_output=True)
        if r.returncode == 0:
            print(f"stopped orphaned players of {g.name}")
        if st.get("status") in ("running", "starting_players", "setup", "starting"):
            st["status"] = "aborted"
            st_path.write_text(json.dumps(st, indent=1))


def teams_command(a) -> int:
    """bin/avalon teams check | demo."""
    from .tournament.series import TOURNAMENT_ROLES
    from .tournament.teams import TeamError, check_team, demo, discover, load_team, words
    base = os.environ.get("AVALON_CWD") or os.getcwd()
    if a.teams_cmd == "demo":
        for d in demo(os.path.join(base, a.out), a.count):
            print(d)
        return 0
    try:
        dirs = [d for path in a.paths for d in discover(os.path.join(base, path))]
    except TeamError as e:
        print(f"avalon teams check: {e}")
        return 1
    if not dirs:
        print(f"no team folders in {', '.join(a.paths)} (folders starting with _ or . are skipped)")
        return 1
    failed = False
    for d in dirs:
        problems, warnings = check_team(d)
        failed = failed or bool(problems)
        detail = "; ".join(problems) if problems else f"{load_team(d).name}, {words((d / 'player.md').read_text())} words"
        print(f"{'FAIL' if problems else 'ok':4}  {d.name:24} {detail}")
        for w in warnings:
            print(f"      {'':24} note: {w}")
    if failed or a.no_game:
        return int(failed)
    print("playing a stand-in game (no model calls) with " + ("this team in every seat" if len(dirs) == 1 else "these teams"))
    for i in range(0, len(dirs), 8):
        group = dirs[i:i + 8]
        seats = {f"P{k + 1}": f"team:{group[k % len(group)]}" for k in range(8)}
        cfg = GameConfig(players=8, roles=TOURNAMENT_ROLES, driver="bots", seats=seats, mock=True, voice=False, max_rounds=2,
                         tag=f"teams-check-{time.strftime('%Y%m%d-%H%M')}")
        try:
            asyncio.run(run_one(cfg))
        except (RuntimeError, ValueError) as e:
            print(f"avalon teams check: the stand-in game stopped: {e}")
            return 1
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="avalon", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run one game")
    add_game_args(r)
    resume = sub.add_parser("resume", help="resume a stopped local Codex game without changing its model or accepted moves")
    resume.add_argument("game_id")
    resume.add_argument("--max-turns", type=int, help="stop at this total turn count (tests)")
    b = sub.add_parser("batch", help="run many games")
    add_game_args(b)
    b.add_argument("--games", type=int, default=10)
    b.add_argument("--parallel", type=int, default=1)
    d = sub.add_parser("dashboard", help="serve the live dashboard")
    d.add_argument("--port", type=int, default=8777)
    d.add_argument("--host", default="127.0.0.1")
    d.add_argument("--public", action="store_true", help="spectator mode for a tournament: public data only, "
                   "private details of a game once it ends, no match launcher")
    d.add_argument("--tag", help="with --public: show only this tournament series")
    d.add_argument("--reveal-prompts", default="never", metavar="WHEN",
                   help="with --public: when team prompts become public: never (default), now, or an ISO time (UTC)")
    e = sub.add_parser("export", help="export one game as a single self-contained HTML file")
    e.add_argument("game_id")
    e.add_argument("--out")
    e.add_argument("--public-only", action="store_true", help="omit roles, private notes and transcripts")
    es = sub.add_parser("export-site", help="multi-game dashboard site (page + per-game data + voices)")
    es.add_argument("--featured")
    es.add_argument("--games", nargs="*")
    es.add_argument("--tournament", nargs="?", const="", metavar="TAG",
                    help="public site for a tournament series (default: the newest): standings plus replays")
    es.add_argument("--out", default="site", help="with --tournament: output folder (default site/)")
    es.add_argument("--reveal-prompts", action="store_true", help="with --tournament: publish the team prompts")
    au = sub.add_parser("audit", help="isolation and compliance report for a game")
    au.add_argument("game_id")
    st = sub.add_parser("stats", help="aggregate stats over finished games")
    st.add_argument("--tag")
    st.add_argument("--driver")
    st.add_argument("--include-mock", action="store_true")
    sub.add_parser("list", help="list games")
    ar = sub.add_parser("arena", help="models play each other in balanced seatings; prints a leaderboard")
    ar.add_argument("--models", default=None, help="comma-separated models.toml keys (default: Haiku 5.5, "
                    "GPT-6 Luna, Qwen3.8 Flash Next); bots may join: scripted, random, bots/x.py")
    ar.add_argument("--players", type=int, default=6, help="5-10 (6 gives three models two seats each)")
    ar.add_argument("--games", type=int, default=1)
    ar.add_argument("--parallel", type=int, default=1, help="games at once")
    ar.add_argument("--seed", type=int)
    ar.add_argument("--tag")
    ar.add_argument("--mock", action="store_true", help="stand-in models: a free dry run of every backend")
    ar.add_argument("--timeout", type=float, default=900, help="seconds per turn")
    ar.add_argument("--max-rounds", type=int, default=3, help="discussion rounds per proposal (max 10)")
    ar.add_argument("--roles", help="comma-separated role keys, one per player (default: the standard set for "
                    "the table size), e.g. merlin,percival,servant,servant,servant,morgana,assassin,oberon")
    ar.add_argument("--huddle-rounds", type=int, default=1,
                    help="rounds of Evil's private huddle before the Assassin names Merlin (0-10, 0 for none)")
    ar.add_argument("--no-reflect", action="store_true")
    lb = sub.add_parser("leaderboard", help="results by model / bot")
    lb.add_argument("--tag")
    lb.add_argument("--include-mock", action="store_true")
    lb.add_argument("--json", action="store_true")
    rl = sub.add_parser("relay", help="turns waiting for a relay-backend reply (e.g. from Claude Code subagents)")
    rl.add_argument("--wait", type=float, default=0, help="block up to this many seconds for a waiting turn")
    rl.add_argument("--audit", metavar="GAME_ID", help="check the subagents that played GAME_ID touched only their own turn files")
    rl.add_argument("--transcripts", metavar="DIR", help="with --audit: Claude Code's subagents/ transcript folder")
    mo = sub.add_parser("models", help="configured models and whether each can play on this machine")
    mo.add_argument("keys", nargs="*", help="only these models.toml keys")
    mo.add_argument("--ping", action="store_true", help="send each ready model one tiny real request")
    tm = sub.add_parser("teams", help="tournament submissions: check them, or make a demo field from the baselines")
    tsub = tm.add_subparsers(dest="teams_cmd", required=True)
    tc = tsub.add_parser("check", help="check format and length caps, then play one stand-in game with the teams")
    tc.add_argument("paths", nargs="*", default=["teams"], help="team folders or folders of teams (default teams/)")
    tc.add_argument("--no-game", action="store_true", help="only check the files")
    td = tsub.add_parser("demo", help="copy the baselines into N team folders, for dry runs")
    td.add_argument("out")
    td.add_argument("--count", type=int, default=8)
    to = sub.add_parser("tournament", help="one seat per team, roles balanced over the series; prints standings")
    to.add_argument("--teams", default="teams", help="folder of team folders (default teams/)")
    to.add_argument("--games", type=int, default=8)
    to.add_argument("--model", default="haiku-5.5-or", help="models.toml key every team plays with")
    to.add_argument("--parallel", type=int, default=1, help="games at once")
    to.add_argument("--seed", type=int, help="makes the schedule (seats and roles) reproducible")
    to.add_argument("--tag")
    to.add_argument("--mock", action="store_true", help="stand-in model: a free dry run")
    to.add_argument("--timeout", type=float, default=600, help="seconds per turn before a safe default move")
    to.add_argument("--max-rounds", type=int, default=3, help="discussion rounds per proposal (max 10)")
    to.add_argument("--no-reflect", action="store_true")
    sd = sub.add_parser("standings", help="tournament standings by team")
    sd.add_argument("--tag", help="the series (default: the newest)")
    sd.add_argument("--json", action="store_true")
    tj = sub.add_parser("trajectories", help="JSONL, one record per seat-turn of the prompt players")
    tj.add_argument("game_ids", nargs="+")
    tj.add_argument("--team", help="only this team's seats")
    tj.add_argument("--out", help="file to write (default: standard output)")
    sub.add_parser("cleanup", help="stop the players of any game whose referee is no longer running")
    a = ap.parse_args(argv)

    if a.cmd in ("run", "batch") and driver_of(a) == "claude" and os.geteuid() != 0:
        sys.exit("The Claude seat backend requires Linux root. For bot games use --seat P1=bots/example_bot.py "
                 "or --driver scripted; for local Codex players use --driver codex.")

    if a.cmd in ("run", "resume"):
        try:
            if a.cmd == "resume":
                asyncio.run(run_one(resume_game=a.game_id, max_turns=a.max_turns))
            else:
                asyncio.run(run_one(cfg_from(a)))
        except (RuntimeError, ValueError, FileExistsError, asyncio.CancelledError) as e:
            sys.exit(f"avalon: game aborted: {e or 'interrupted'}")
    elif a.cmd == "batch":
        asyncio.run(run_batch(a))
    elif a.cmd == "dashboard":
        from .dashboard.server import serve
        serve(a.host, a.port, public={"tag": a.tag, "reveal": a.reveal_prompts} if a.public else None)
    elif a.cmd == "export":
        from .dashboard.export import export_game
        print(export_game(a.game_id, a.out, public_only=a.public_only))
    elif a.cmd == "export-site" and a.tournament is not None:
        from .tournament.publish import export_site
        out = os.path.join(os.environ.get("AVALON_CWD") or os.getcwd(), a.out)
        try:
            print(json.dumps(export_site(a.tournament or None, out, reveal_prompts=a.reveal_prompts), indent=1))
        except ValueError as e:
            sys.exit(f"avalon export-site: {e}")
    elif a.cmd == "export-site":
        from .dashboard.export import export_site
        print(json.dumps(export_site(a.featured, a.games), indent=1))
    elif a.cmd == "audit":
        from .audit import audit_game
        print(json.dumps(audit_game(GAMES / a.game_id), indent=1))
    elif a.cmd == "stats":
        from .stats import compute_stats
        print(json.dumps(compute_stats(tag=a.tag, driver=a.driver, include_mock=a.include_mock), indent=1))
    elif a.cmd == "cleanup":
        cleanup()
    elif a.cmd == "arena":
        from .arena import DEFAULT_MODELS, compute_leaderboard, format_leaderboard, run_arena
        models = [m.strip() for m in (a.models or DEFAULT_MODELS).split(",") if m.strip()]
        try:
            tag, gids = asyncio.run(run_arena(models, a.players, a.games, parallel=a.parallel, seed=a.seed, tag=a.tag,
                                              mock=a.mock, timeout=a.timeout, max_rounds=min(10, a.max_rounds),
                                              huddle_rounds=max(0, min(10, a.huddle_rounds)), reflect=not a.no_reflect,
                                              roles=a.roles.split(",") if a.roles else None))
        except (RuntimeError, ValueError) as e:
            sys.exit(f"avalon arena: {e}")
        if gids:
            print("\n" + format_leaderboard(compute_leaderboard(tag=tag, include_mock=a.mock)))
    elif a.cmd == "teams":
        sys.exit(teams_command(a))
    elif a.cmd == "tournament":
        from .tournament.series import compute_standings, format_standings, run_tournament
        from .tournament.teams import TeamError, load_teams
        try:
            teams = load_teams(os.path.join(os.environ.get("AVALON_CWD") or os.getcwd(), a.teams))
            tag, gids = asyncio.run(run_tournament(teams, a.games, model=a.model, parallel=a.parallel, seed=a.seed,
                                                   tag=a.tag, mock=a.mock, timeout=a.timeout,
                                                   max_rounds=min(10, a.max_rounds), reflect=not a.no_reflect))
        except (RuntimeError, ValueError, TeamError) as e:
            sys.exit(f"avalon tournament: {e}")
        print("\n" + format_standings(compute_standings(tag)))
    elif a.cmd == "standings":
        from .tournament.series import compute_standings, format_standings
        st = compute_standings(a.tag)
        print(json.dumps(st, indent=1) if a.json else format_standings(st))
    elif a.cmd == "trajectories":
        from .tournament.publish import trajectories
        out = open(os.path.join(os.environ.get("AVALON_CWD") or os.getcwd(), a.out), "w") if a.out else sys.stdout
        n = 0
        try:
            for rec in trajectories(a.game_ids, team=a.team):
                out.write(json.dumps(rec) + "\n")
                n += 1
        except FileNotFoundError as e:
            sys.exit(f"avalon trajectories: unknown game {e}")
        if a.out:
            out.close()
            print(f"{n} seat-turn record(s) -> {a.out}")
    elif a.cmd == "leaderboard":
        from .arena import compute_leaderboard, format_leaderboard
        lb = compute_leaderboard(tag=a.tag, include_mock=a.include_mock)
        print(json.dumps(lb, indent=1) if a.json else format_leaderboard(lb))
    elif a.cmd == "relay" and a.audit:
        from .models import audit_relay
        if not a.transcripts:
            sys.exit("--audit needs --transcripts DIR (e.g. ~/.claude/projects/<project>/<session>/subagents)")
        report = audit_relay(a.audit, a.transcripts)
        print(json.dumps(report, indent=1))
        sys.exit(0 if report["clean"] and report["subagents"] else 1)
    elif a.cmd == "relay":
        from .models import pending_relay
        deadline = time.time() + a.wait
        while True:
            pending = pending_relay()
            if pending:
                for req in pending:
                    print(f"{req}  ->  {req.with_name(req.name.replace('.request.md', '.reply.txt'))}")
                return
            live = [g for g in GAMES.iterdir() if (g / "referee" / "status.json").exists() and json.loads(
                (g / "referee" / "status.json").read_text()).get("status") in ("setup", "starting_players", "running")]
            if not live:
                print("no game is running")
                sys.exit(2)
            if time.time() >= deadline:
                print("nothing waiting yet")
                sys.exit(3)
            time.sleep(1)
    elif a.cmd == "models":
        from .models import load_models, ping, preflight
        models = load_models()
        unknown = [k for k in a.keys if k not in models]
        if unknown:
            sys.exit(f"unknown model(s): {', '.join(unknown)}; models.toml defines: {', '.join(models)}")
        failed = False
        for key, m in models.items():
            if a.keys and key not in a.keys:
                continue
            problem = preflight(m)
            status = "ready" if not problem else problem
            if a.ping and not problem:
                ok, detail = asyncio.run(ping(m))
                status, failed = (f"answered ({detail})" if ok else f"NO ANSWER: {detail}"), failed or not ok
            failed = failed or bool(problem and a.keys)
            print(f"{key:24} {m.label:20} {m.backend:10} {status}", flush=True)
        if failed:
            sys.exit(1)
    elif a.cmd == "list":
        for g in sorted(GAMES.iterdir()):
            st = g / "referee" / "status.json"
            if st.exists():
                s = json.loads(st.read_text())
                print(f"{g.name:48} {s.get('status', '?'):10} {s.get('winner') or '':5} {s.get('phase') or ''}")


if __name__ == "__main__":
    main()
