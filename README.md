# Avalon prompt arena

Teams write the strategy prompt for an AI player of *The Resistance: Avalon*. Every player is the same model,
**Haiku 5.5 at medium reasoning effort** (through OpenRouter), so the only thing that differs between teams
is the prompt. Eight teams sit at one table, one seat each. A tournament rotates the roles so every team
plays every role equally often, and ranks the teams.

You need Python 3.11 or newer and one [OpenRouter](https://openrouter.ai) key. There is nothing to install.

- [Quick start](#quick-start)
- [The game](#the-game)
- [What a solution is](#what-a-solution-is)
- [How your prompt is used](#how-your-prompt-is-used)
- [Improve on the baseline](#improve-on-the-baseline)
- [Submit](#submit)
- [How solutions are evaluated](#how-solutions-are-evaluated)
- [Cost and time](#cost-and-time)

## Quick start

```bash
git clone https://github.com/nelionel/avalon-arena.git
cd avalon-arena
export OPENROUTER_API_KEY=sk-or-...            # or write OPENROUTER_API_KEY=sk-or-... into secrets/credentials.env
bin/avalon models --ping haiku-5.5-or          # one tiny real request: checks the key
```

Make your team from a baseline and check it:

```bash
cp -r teams/_baselines/deducer teams/my-team    # then edit player.md, and your name in team.toml
bin/avalon teams check teams/my-team            # format, word limit, and one free game against a stand-in model
```

Play real games against the baselines (about $0.20 and 5 to 7 minutes per game):

```bash
bin/avalon teams demo games/field --count 7     # 7 baseline copies
cp -r teams/my-team games/field/
bin/avalon tournament --teams games/field --games 8 --parallel 2 --tag my-test
bin/avalon standings --tag my-test
bin/avalon dashboard                            # watch and replay at http://127.0.0.1:8777
```

`--mock` on `tournament` plays the same series against a free stand-in model that makes random legal moves.
It checks the plumbing, not the strategy.

## The game

The Resistance: Avalon is a hidden-role game for 5 to 10 players. The official rules are on the publisher's
page, [Indie Boards & Cards: The Resistance: Avalon](https://indieboardsandcards.com/our-games/the-resistance-avalon/),
and the rulebook is also in the Files section of its
[BoardGameGeek page](https://boardgamegeek.com/boardgame/128882/the-resistance-avalon).

This arena always plays the same setup:

| | |
|---|---|
| Players | 8, one per team |
| Good (5) | Merlin, Percival, 3 Loyal Servants of Arthur |
| Evil (3) | Morgana, the Assassin, Oberon |
| Quest team sizes | 3, 4, 4, 5, 5. Quest 4 fails only with 2 fail cards; the others fail with 1 |
| Win | Good: pass 3 quests and survive the Assassin. Evil: fail 3 quests, get 5 proposals in one quest rejected, or have the Assassin name Merlin after Good passes 3 quests |

Who knows what at the start:

- **Merlin** sees all three Evil players.
- **Percival** sees Merlin and Morgana, without knowing which is which.
- **Morgana and the Assassin** see each other. **Oberon** is Evil but sees no one, and they don't see Oberon.
- **Loyal Servants** know nothing.

Each quest runs like this. These are the official rules, with talk made into turns so models can play:

1. **Proposal.** The leader proposes a team and may add a public pitch.
2. **Discussion**, at most 3 rounds. In each round every player at once posts one public message (at most
   300 words) or passes, and says whether they are ready to vote. Messages of a round are published together.
   The leader may change the team. Discussion ends when everyone is ready, when a round passes in silence, or
   after round 3.
3. **Vote.** Everyone votes at the same time; votes are public. A strict majority sends the team. Otherwise
   the next player leads; a fifth rejection in one quest wins for Evil.
4. **Quest.** Team members secretly play success or fail. Good must play success. Only the count of fails is
   revealed.

If Good passes 3 quests, the Evil players confer privately for one round (each posts a message and a
suggested target), then the Assassin names one player. Naming Merlin wins for Evil. Lady of the Lake is not
used. [RULES.md](RULES.md) has the exact rules text every player receives.

## What a solution is

A folder with one required file:

```
teams/<team-id>/player.md     your prompt: Markdown, at most 1,500 words
teams/<team-id>/team.toml     optional: name = "Display name", color = "#3b82f6"
```

The folder name is your team id: lowercase letters, digits, `-` and `_`. `bin/avalon teams check` checks all of
this. Only the prompt is yours. The model, its reasoning effort, the token limits, the rules and the reply
format are the same for everyone, and the harness rules take precedence over your prompt.

## How your prompt is used

There is no chat history. Every turn is one fresh request:

- **System** (the same all game, so it is cached): the rules, how turns work, your seat's secret role card,
  then your `player.md`.
- **User**: your memory, the public events since your last turn, the board (score, finished quests,
  rejections, team sizes, leader order), the decision to make, the turn facts and the reply format.

The reply ends with one JSON object: the move, a private `reason`, `beliefs` (the probability that each other
player is Evil) and `memory`. `memory` is your full rewritten notebook, at most 800 words, or `null` to keep it
unchanged. Anything the memory leaves out is gone next turn, so a good prompt says exactly what to keep in it.

Other players' words always appear as quoted JSON strings after their seat (`P3: "..."`), so nobody can pose
as the referee. An invalid reply gets the errors back and two more tries. After that the referee plays a safe
default move (pass, approve, play success, or propose yourself and the next seats) and records a fault for
your team.

## Improve on the baseline

[teams/_baselines](teams/_baselines) has four starting points:

| Baseline | Idea |
|---|---|
| `minimal` | the essentials in 250 words |
| `deducer` | a structured notebook and explicit belief updates |
| `cautious-merlin` | keeping Merlin hidden from the Assassin |
| `coordinated-evil` | conventions Evil players can follow without talking |

[baseline/](baseline) holds real games of these four, two copies each, all eight seats played by Haiku 5.5 at
medium effort:

- `baseline/replays/<game>.html`: a single-file replay of each game. Open it in a browser and click a seat
  to see its role, private reasons, memory, beliefs and reasoning, turn by turn.
- `baseline/trajectories.jsonl`: one JSON record per seat-turn, with the exact input sections (harness, role
  card, team prompt, memory, events, board, decision, facts), the reply, the move, memory before and after,
  the model's reasoning, tokens and cost, rejected attempts, and the game's outcome for that seat.
- `baseline/standings.txt`: the result of the two games. In `e865` Good passed three quests and Merlin
  survived the Assassin; in `ed91` Evil failed three quests. Neither game had a fault.

Make trajectories of your own games the same way:

```bash
bin/avalon trajectories GAME_ID ... --team my-team --out mine.jsonl
```

## Submit

Pick one:

- **Public:** open a pull request that adds `teams/<team-id>/`. Everyone can read your prompt.
- **Private until the final:** put `player.md` (and `team.toml`) in a
  [secret gist](https://gist.github.com) and send the gist's URL to the organizers. A secret gist is only
  visible to people who have its URL. The organizers fetch it with
  `git clone https://gist.github.com/<gist-id>.git teams/<team-id>`.

Run `bin/avalon teams check teams/<team-id>` before you submit.

## How solutions are evaluated

The organizers run:

```bash
bin/avalon teams check teams/
bin/avalon tournament --teams teams/ --games 16 --model haiku-5.5-or --parallel 2 --seed <announced> --tag final
bin/avalon standings --tag final
```

| Setting | Value |
|---|---|
| Model | `haiku-5.5-or`: `anthropic/claude-haiku-5.5` through OpenRouter, `reasoning = { effort = "medium" }`, at most 16,000 tokens per reply, prompt caching on ([models.toml](models.toml)) |
| Table | exactly 8 teams per series. With fewer entries the field is filled with baseline copies; with more, the organizers split the teams into pools of 8, one series per pool |
| Roles | each block of 8 games is a shuffled Latin square: every team holds each of the 8 role slots once. 16 games give every team every role twice (10 Good games, 6 Evil) |
| Seats | shuffled every game; `--seed` makes the whole schedule reproducible |
| Discussion | at most 3 rounds per proposal; 1 Evil huddle round before the Assassin's shot |
| Turn limit | 600 seconds per turn, then a safe default move (counted as a provider error, not a team fault) |
| Memory | players keep nothing between games |
| Score | the mean of the team's Good win rate and Evil win rate, so both sides count equally |
| Tie-breaks | Evil read (as a Good player other than Merlin: average final P(Evil) given to the real Evil players minus that given to Good players), then Merlin survival, then Assassin accuracy, then fewer faults |

Stopped games do not count. [TOURNAMENT.md](TOURNAMENT.md) has the details, including the live spectator site
and a static site for GitHub Pages.

## Cost and time

Measured on the baseline games: $0.17 and $0.21 per 8-player game (29 and 37 turns), 5 to 7 minutes each,
with two games at once. OpenRouter reports the cost of every move; `bin/avalon standings` shows dollars per
team per game. A 16-game tournament costs about $3.50.

## More

- [TOURNAMENT.md](TOURNAMENT.md): the tournament in detail, hosting a live or static spectator site.
- [ARENA.md](ARENA.md): other models (GPT-6 Luna, Qwen, Haiku through other backends) and a model leaderboard.
- [CONTRIBUTING.md](CONTRIBUTING.md): Python bots instead of prompts.
- [HARNESS.md](HARNESS.md): the referee, sandboxes and the original Claude Code and Codex drivers.

Tests run without keys: `python3 tests/test_tournament.py`, `python3 tests/test_arena.py`.
