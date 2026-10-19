# Avalon for Codex and Claude Code agents

A referee runs games of *The Resistance: Avalon* (5-10 players) with either
**GPT-6 Luna through local Codex sessions** or **Haiku 5.5 through interactive
Claude Code TUIs**. Each seat keeps separate context, private memory and
decision files. Both backends share the rules, turn barrier, validator and
dashboard. The two backends have different sandbox boundaries, described below.

**Model arena:** Haiku 5.5, GPT-6 Luna and Qwen3.8 Flash Next at one table, with a
leaderboard and a match launcher in the dashboard. See [ARENA.md](ARENA.md).

**Writing a bot?** Start with [CONTRIBUTING.md](CONTRIBUTING.md). Bots are plain
Python classes that play on macOS or Linux without root, installs or API keys.

```
QWEN_SSH=<gpu-box> bin/three-way              # Haiku 5.5 vs GPT-6 Luna vs Qwen3.8 Flash Next (MOCK=1: free dry run)
bin/avalon run --players 7 --seat P1=bots/example_bot.py   # your Python bot vs scripted bots (no root)
bin/avalon run --players 7 --driver codex --model gpt-6-luna  # local ChatGPT sign-in
bin/avalon resume <game_id>                 # interrupted durable Codex game; same players/model
bin/avalon run --players 7 --mock             # dry run: real Claude Code TUIs, local mock model (free)
bin/avalon run --players 7                    # real run with claude-haiku-5-5
bin/avalon run --players 7 --driver scripted  # heuristic bots, no Claude (seconds)
bin/avalon batch --games 20 --parallel 2      # many games
bin/avalon stats --tag <tag>                  # aggregate results
bin/avalon dashboard                          # http://127.0.0.1:8777 (live)
bin/avalon export <game_id>                   # single-file HTML replay (shareable)
bin/avalon audit <game_id>                    # isolation + compliance report
tests/jail_probe.sh                           # kernel sandbox probe (20 checks)
```

The Codex backend runs as the current local user, using a separate enforced
filesystem permission profile per seat. It never copies login credentials
into a player folder. The original Claude TUI backend runs as root on Linux
and creates per-seat uids and jails under `/opt/avalon`; it uses uids
61001-61200 and ports 18080+ for its mock model. The dashboard defaults to
localhost port 8777 and accepts `--port` to choose another port.

## Credentials for real runs

Codex players use the local Codex ChatGPT login. The following credential
instructions apply only to the Claude backend.

Players need their own credential (this container's Claude Code session is
authenticated by the desktop app, which does not hand its token to child
processes). Put **one** of these in `secrets/credentials.env` (`chmod 600`):

```
ANTHROPIC_API_KEY=sk-ant-...
# or, to use a Claude subscription: run `claude setup-token` anywhere and paste
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
```

The referee copies it into a root-only env file per seat; it never appears on
a command line.

## How a game runs

```
referee (root) ──writes──> public/ (git repo, read-only to players, one commit per turn)
     │                      RULES.md  state.json  updates/NNNN.md  log.md  events.jsonl
     │
     ├─ tmux (root socket) ─> seat P1: avalon-jail -> claude (TUI, uid 61001)
     │                         private: players/p1/work/{ROLE.md, inbox/, notes/, outbox/}
     ├─ ...                    one interactive Claude Code session per seat, for the whole game
     └─ seat P10
```

1. **Onboarding.** Roles are shuffled with `SystemRandom`. Each seat gets a
   private role card (`ROLE.md`, with its night-phase knowledge: Merlin sees
   Evil except Mordred, Percival sees Merlin+Morgana, Evil see each other except
   Oberon) and a private `CLAUDE.md` appended to its system prompt, so the role
   survives compaction.
2. **Every turn is a barrier.** The referee writes each actor's private turn file
   (`inbox/turn_NNNN.md`), types one line into every actor's TUI at the same
   time, waits until **all** of them have submitted, snapshots every private
   folder into a referee-only git repo, validates, applies the moves, and only
   then publishes the turn to the public repo in a single commit. No player sees
   anything from a turn until every submission for it is in.
3. **Each turn requires three things**, enforced by a Stop hook inside the
   player's own Claude Code (it refuses to let the turn end until they are done):
   - the player read the public repo (`state.json`) during this turn;
   - a private decision document `notes/turn_NNNN.md` (at least 20 words) explaining the choice;
   - a valid submission `outbox/turn_NNNN.json`; public messages are at most 300 words.
4. **Per proposal:** leader proposes, then up to 10 simultaneous discussion rounds
   (the leader may amend the team), ending early when everyone is ready to vote or a
   round passes in silence. Then a simultaneous public vote, then secret quest cards
   (only the fail count is published). If Good wins three quests, Evil gets a private
   huddle (routed only to Evil inboxes) and the Assassin names Merlin. Everyone writes
   a post-game reflection.

Turn files carry a machine-readable spec; `hooks/turnspec.py` is the single
validator used by both the in-jail hook and the referee.

Codex uses the same move/note/memory validator through its driver and the referee;
it does not run Claude hooks. Both model backends stop on invalid or missing
submissions instead of substituting a heuristic move. Codex audit coverage is
limited: shell commands are not a complete file-read trace.

New Codex games keep durable player conversations. Private receipts save each
accepted move, then an immutable turn transaction is written before publication.
`avalon resume` keeps the same model, roles and player session IDs, retries only
unfinished players, and reconciles interrupted publication without duplicate
turns. A per-game lock prevents two referees from owning one game. Original
Claude TUI in-session restart is preserved; full-referee resume currently supports
Codex games only. Older temporary Codex demos remain replayable.

## Isolation model

Docker is not available here and the container forbids namespaces, so the
"container" is built from primitives that work without them. Each layer is
tested independently.

| Layer | What it enforces | Verified by |
|---|---|---|
| Own Unix uid per seat | private folders `700`; seats cannot read, signal or ptrace each other | `tests/jail_probe.sh`, `/proc` facts on the dashboard |
| Landlock (kernel, ABI 4) | read/write only the seat's own folder; read-only public repo, runtime, system dirs; **TCP connect only to the API port**, no TCP bind | `jail_probe.sh` incl. a probe as the *owner* uid under another seat's policy |
| seccomp | no `bind`/`listen` (no servers of any kind), no SysV/POSIX IPC, ptrace, keyrings, namespaces, mounts, io_uring | `jail_probe.sh` (UDP/abstract-socket bind, shm) |
| Claude Code permissions | `--restricted` (file tools confined to the working dirs, user/project settings ignored), fixed `--tools`, `dontAsk` mode (nothing ever prompts; anything not allowed is denied), explicit allow/deny rules | adversarial dry run with the hook guard disabled |
| Hook path guard | PreToolUse hook re-checks every file path | adversarial dry run |
| Referee | barrier per turn, aggregated quest cards, Evil huddle routed privately, nothing secret on any command line | audit checks below |

Allowed tools: Read, Write, Edit, Glob, Grep, TodoWrite and a **read-only Bash
allowlist** (`ls cat head tail wc grep sort uniq diff git log/show/diff date pwd`).
Writes are allowed only to `notes/`, `outbox/`, `scratch/` and `journal.md`. No
WebFetch/WebSearch, subagents, MCP or skills.

**Audit** (`avalon audit`, also on the dashboard's Verification tab), from each
seat's own transcript and hook log:
- no successful access outside the seat's folder and the public repo;
- **canary tokens**: every `ROLE.md` contains a random token; it must never appear in
  another seat's transcript or anywhere in the public repo's history;
- no instruction files loaded from outside the seat;
- each claude process ran as its seat uid with `no_new_privs` and a seccomp filter;
- public events never reveal who played which quest card or the huddle;
- every message at most 300 words; every turn published only after all submissions;
- every submission has a private decision document; no submission was defaulted.

`--mock --adversarial` makes every agent try six break-outs during onboarding
(read another seat's role, glob all seats, write to the public repo, `cat` and `ls`
via Bash, fetch the dashboard). All are denied; the dashboard lists each attempt and
the layer that stopped it.

Residual surface, stated plainly: system directories (`/usr`, `/etc`, `/proc`) are
readable because a process cannot run without them (they hold no game data), and
other processes' command lines are visible in `/proc` (nothing secret is ever on a
command line). TCP 443 reaches the internet as the API requires; the tools that could
use it (WebFetch, arbitrary Bash) are not available.

## Dashboard

`bin/avalon dashboard` serves a live view on `127.0.0.1:8777` (the players' Landlock
policy cannot reach it). Over SSH: `ssh -L 8777:127.0.0.1:8777 <host>`.

- **Quest board** with a replay slider: step through the game turn by turn.
- **Castle artwork** from the supplied `avalon-svg-package.zip`, with mission
  outcomes, the active mission, team sizes, fail thresholds and rejection count
  driven by the selected replay moment. Characters surround the board on desktop
  and move below it on narrow screens. The renderer is embedded in exports.
- **The table**: every seat's role, live state (what tool it is running right now),
  tool calls and blocked attempts.
- **Chronicle**: proposals, simultaneous discussion rounds, votes, quests; in the
  referee view each message has the player's private reasoning for that turn.
- **Terminals**: a live mirror of every player's real Claude Code TUI.
- **Verification**: the audit, per-seat sandbox evidence, blocked attempts, and a
  barrier-timing chart showing every submission landed before the public commit.
- **Statistics** across finished games.
- **Seat dossier**: per-turn submissions and decision documents, the full transcript,
  the hook log, the role card, and private memory from the selected replay turn.
  **Player-blind** view hides everything private.

`bin/avalon export <game_id>` writes a new timestamped HTML file under the game
directory, with data, character animations and available recorded voices embedded.
It works offline. Use `--public-only` to omit private notes, beliefs, memory,
transcripts, configuration and huddle membership. The normal export is a referee
replay containing private game information. Existing exports are not overwritten.

The original board package is preserved in `assets/board_src`. After editing its
renderer or vector data, run `python3 assets/build_board.py`; the dashboard reads
the generated `avalon/dashboard/static/board.js` on the next page load. Run
`.tools/venv/bin/python tests/test_board_ui.py` to check the board against engine
transitions for all 5–10 player setups, including mobile layouts and exports.

## Layout

```
avalon/engine.py      rules state machine (pure)        avalon/referee.py   orchestrator, barrier, defaults
avalon/roles.py       roles, setups, knowledge          avalon/seat.py      per-seat provisioning + launch
avalon/prompts.py     every text agents read            avalon/publicrepo.py public git repo
avalon/drivers/       claude_tui (tmux) and scripted    avalon/audit.py     isolation verification
avalon/mockapi.py     mock Messages API for dry runs    avalon/stats.py     aggregate statistics
avalon/dashboard/     server, export, static/app.html   hooks/              in-jail hook + shared validator
avalon/bot.py         Bot base class for Python bots    bots/               contributed bots (example_bot.py)
avalon/models.py      models.toml registry + backends   avalon/arena.py     arena series + leaderboard
avalon/drivers/model_player.py  model seats            avalon/drivers/local_bot.py  bot seats (no root)
avalon/arena_mock.py  stand-in models for dry runs     models.toml         the models that can take a seat
jail/avalon-jail.c    the sandbox launcher              runtime/            pinned claude binary, jail
games/<id>/           public/, players/pN/, referee/ (roles, audit repos, screens, logs)
```
