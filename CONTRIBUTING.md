# Writing an Avalon bot

This repo runs games of *The Resistance: Avalon* for 5 to 10 players. You write a
player as a small Python class, then play it against the built-in bots and
everyone else's.

## Quick start

You need Python 3.11 or newer, Git, and macOS or Linux. Root, `pip install` and
API keys are not needed.

```bash
git clone https://github.com/nelionel/avalon.git
cd avalon
bin/avalon run --players 7 --seat P1=bots/example_bot.py --seed 1
```

The last line prints the game ID and the winner. To watch the game, start the
dashboard and open http://127.0.0.1:8777:

```bash
bin/avalon dashboard
```

Pick the game and use the replay slider. The referee view shows every seat's
role, private reasoning, beliefs and memory turn by turn.

## Make your own bot

```bash
cp bots/example_bot.py bots/yourname.py
```

In the copy, rename the class and set `name = "yourname"`. Then play it:

```bash
bin/avalon run --players 7 --seat P1=bots/yourname.py
```

The smallest working bot is three lines, because the base class plays a random
legal move for every turn you don't override:

```python
from avalon.bot import Bot

class Yourname(Bot):
    name = "yourname"
```

## Running games

| Flag | Meaning |
|---|---|
| `--seat P1=bots/you.py` | Who plays a seat. Repeat for more seats. A value can be a `.py` file, `FILE.py:ClassName` when the file has several bots, `random` or `scripted`. Paths are relative to where you run the command. |
| `--fill BOT` | Who plays the seats that `--seat` leaves out. The default is `scripted`, the built-in heuristic bot. |
| `--players N` | 5 to 10 seats (the default is 7). |
| `--seed N` | Seeds the role shuffle and every bot's `self.rng`, so the same command replays the same game. |
| `--roles merlin,percival,...` | A custom role list instead of the standard one for that table size. |

Roles are dealt at random, so play many games before judging a bot:

```bash
bin/avalon batch --games 50 --seat P1=bots/you.py --tag you-v1
bin/avalon leaderboard --tag you-v1
```

`bin/avalon leaderboard` ranks every bot and model by win rate overall and by side,
using each game's `games/<id>/referee/summary.json` (which bot played which seat, the
roles and the winner).

Your bot can also face AI models (Haiku 5.5, GPT-6 Luna, Qwen3.8 Flash Next) in an
arena series, for example `bin/avalon arena --models qwen-3.8-flash-next,bots/you.py`.
See [ARENA.md](ARENA.md).

## The bot API

Subclass `avalon.bot.Bot`. These attributes are set before `setup()` runs:

| Attribute | Example | |
|---|---|---|
| `self.player` | `"P3"` | your seat |
| `self.role` | `"percival"` | see the role table below |
| `self.side` | `"good"` or `"evil"` | |
| `self.knowledge` | `{"role": ..., "side": ..., "merlin_candidates": ["P1", "P6"], ...}` | what your role saw at night |
| `self.players` | `["P1", ..., "P7"]` | every seat |
| `self.rng` | `random.Random` | use this for randomness so `--seed` replays games |

`self.knowledge` has only the keys your role gets:

| Role | Extra keys |
|---|---|
| Merlin | `sees_evil`: Evil players except Mordred. `hidden_from_you`: `["Mordred"]` when Mordred is in play. |
| Percival | `merlin_candidates`: Merlin and Morgana, in seat order. `morgana_in_play`. |
| Evil except Oberon | `evil_teammates`: the other Evil players except Oberon. `oberon_in_play`. |
| Oberon | `evil_teammates`: always `[]`. |
| Loyal Servant | nothing extra |

Each turn, the referee calls `act(turn, state)`, which calls the method named
after the turn kind. Return a dict with the move:

| Method | When | Return |
|---|---|---|
| `onboard` | once, at the start | `{"ack": True}` |
| `propose` | you lead | `{"team": ["P1", "P4"], "message": str or None}` with exactly `turn["team_size"]` players |
| `discuss` | every round of discussion of a proposed team | `{"message": str or None, "ready_to_vote": bool}`. The leader (`turn["can_amend"]`) may add `"team"` to amend the proposal. |
| `vote` | after discussion | `{"vote": "approve"}` or `{"vote": "reject"}` |
| `quest` | you are on an approved team | `{"card": "success"}` or `{"card": "fail"}`. `turn["options"]` lists what you may play; Good may only succeed. |
| `evil_huddle` | Good won three quests and you are Evil | `{"message": str or None, "suggested_target": "P2" or None}` |
| `assassinate` | you are the Assassin, after the huddle | `{"target": "P2", "message": str or None}` |
| `reflect` | game over | `{"message": str or None}` |

On any move you may also add `"reason"`: private text that the replay shows
as your decision notes. Public messages are limited to 300 words. Leave out
`"turn"` and `"beliefs"`, which the adapter fills in.

Optional overrides:

- `setup(self)`: runs once before the first turn.
- `beliefs(self, turn, state)`: returns `{"P2": 0.7, ...}`, your probability that each other player is Evil. It is recorded on most turns and charted in the replay. The default uses only your night knowledge.
- `memory(self)`: text saved as your private memory after each turn, shown in the replay.
- `act(self, turn, state)`: handles every turn kind yourself.

### `turn`

`turn` always has `kind`, `turn`, `you`, `players`, `quest` (1 to 5), `proposal`
(1 to 5 within the quest), `leader`, `team_size`, `fails_required` and
`evil_total`. Other keys depend on the kind:

| Kind | Extra keys |
|---|---|
| `discuss` | `team`, `round`, `max_rounds`, `can_amend` |
| `vote` | `team`, `rejections_so_far` (a fifth rejection in a row is an Evil win) |
| `quest` | `team`, `options` |
| `evil_huddle`, `assassinate` | `candidates` (the Good players), `assassin`, `evil_team`, `huddle_round`, `huddle_rounds`, `huddle` (the huddle so far: `[{"player", "round", "message", "suggested_target"}]`) |
| `reflect` | `roles` (everyone's role), `winner`, `win_reason` |

### `state`

`state` is the public record that every player sees:

- `phase`, `quest`, `proposal`, `leader`, `leader_order`, `team_size`, `fails_required`
- `score`: `{"good": 1, "evil": 0}`
- `quests`: finished quests, as `{"quest", "team", "fails", "fails_required", "result"}`
- `vote_history`: finished votes, as `{"quest", "proposal", "leader", "team", "votes": {"P1": "approve", ...}, "approved"}`
- `proposed_team`, `rejected_proposals_this_quest`, `quest_team_sizes`, `winner`, `win_reason`
- `events`: every public event so far, oldest first. Types are `game_started`, `proposal`, `team_amended`, `discussion` (each round's `entries`: `{"player", "message", "ready"}`), `discussion_closed`, `vote_result`, `quest_result`, `assassination_phase`, `assassination`, `game_over` and `reflections`.

Who played which quest card is never public; only the number of fails is.

### Roles in play

| Players | Good | Evil |
|---|---|---|
| 5 | Merlin, Percival, 1 Servant | Morgana, Assassin |
| 6 | Merlin, Percival, 2 Servants | Morgana, Assassin |
| 7 | Merlin, Percival, 2 Servants | Morgana, Assassin, Oberon |
| 8 | Merlin, Percival, 3 Servants | Morgana, Assassin, Minion |
| 9 | Merlin, Percival, 4 Servants | Morgana, Assassin, Mordred |
| 10 | Merlin, Percival, 4 Servants | Morgana, Assassin, Mordred, Oberon |

The full rules every player gets are in `games/<id>/public/RULES.md` after any game.

## When something goes wrong

The referee never replaces a bad move. An invalid move, or an exception in
your bot, stops the game before anything from that turn is published, and the
error names the seat and the bot:

```
avalon: game aborted: P1 (yourname) made an invalid propose move on turn 2: "team" must have exactly 3 players (got 4)
```

Tracebacks are also saved in `games/<id>/players/p1/audit/bot-errors.log`.

## Fair play

Bots run inside the referee's own process, without a sandbox. Use only what
your methods are given. Don't read files under `games/`, look at other
objects in memory, or use the network. Tournament play will isolate bots;
until then these rules are on trust. Keep moves fast (well under a second)
and single-threaded.

## Tests

```bash
python3 tests/test_engine.py
python3 tests/test_bots.py
python3 tests/test_recovery.py
python3 tests/test_codex_driver.py
python3 tests/test_public_views.py
```

GitHub Actions runs these on Linux and macOS for every push to `main` and every pull request.

## Submitting your bot

Create a branch, add `bots/yourname.py` (or a folder `bots/yourname/` whose
helper modules sit next to your bot file and are imported by plain name), and
open a pull request. Keep other changes in separate pull requests.

## The other backends

The same referee also runs GPT-6 Luna through local Codex sessions and Haiku
through sandboxed Claude Code terminals. That setup, and the Linux root
sandbox it needs, is described in [HARNESS.md](HARNESS.md). You don't need it to write bots.
