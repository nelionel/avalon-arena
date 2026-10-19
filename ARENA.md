# Model arena

AI models play *The Resistance: Avalon* against each other: by default Haiku 5.5,
GPT-6 Luna and Qwen3.8 Flash Next. Any Anthropic or OpenAI-compatible model, and any
Python bot from [CONTRIBUTING.md](CONTRIBUTING.md), can join the same table. Every
model gets the same rules, the same prompts and the same move validator. The replay
shows each model's private reasoning, beliefs and memory, turn by turn.

For the prompt tournament (eight teams, each entering a system prompt and a memory policy), see
[TOURNAMENT.md](TOURNAMENT.md).

## Try it without keys

```bash
bin/avalon arena --mock --games 3
bin/avalon dashboard            # open http://127.0.0.1:8777, then the Arena tab
```

`--mock` swaps every model for a stand-in that plays random legal moves through the
same backend adapters (Anthropic API, OpenAI API, Codex app-server, Claude Code CLI).
It's free and instant, and useful for checking the setup, but says nothing about
strategy.

## The interface

`bin/avalon dashboard` serves everything on http://127.0.0.1:8777:

- **The table.** A colored chip under each character shows which model plays that seat. In the referee view, click a seat or a speech to see the model's role, private reason, reasoning trace and beliefs for that moment.
- **Arena tab.** The leaderboard by model: win rate overall, as Good and as Evil, how often Merlin escapes the Assassin, the Assassin's hit rate, time per move, the share of moves that needed a corrected reply, and games each model stopped. The leaderboard also has an **Evil read** score: for Good players other than Merlin, the average final probability they gave real Evil players minus the probability they gave Good players (1 is perfect, 0 is a coin flip). The tab also has **Start a match**: pick models (and, under "Add bots to the table", the built-in or `bots/` bots), table size, number of games, discussion rounds and Evil huddle rounds. The first game opens as soon as it begins. Only models that are ready on this machine can be ticked, unless you choose a dry run.
- **Terminals tab.** In the referee view, each model seat streams its latest replies, with reasoning dimmed above the JSON answer and "… thinking" while a reply is pending.
- **Game picker.** Each game is listed by its lineup (for example "Haiku 5.5 vs Qwen3.8 Flash Next vs GPT-6 Luna"). Games are live while they run and replayable afterwards. `bin/avalon export GAME_ID` writes a single-file replay.

## Models

`models.toml` lists them. `bin/avalon models` shows which ones can play on this machine.

| Key | Model | Backend | Needs |
|---|---|---|---|
| `haiku-5.5` | Haiku 5.5 | Anthropic Messages API | `ANTHROPIC_API_KEY` |
| `haiku-5.5-cli` | Haiku 5.5 | your signed-in Claude Code (`claude -p`) | `claude` on PATH, signed in |
| `gpt-6-luna` | GPT-6 Luna | your signed-in Codex app-server | Codex on PATH (or the ChatGPT app), signed in |
| `gpt-6-luna-api` | GPT-6 Luna | OpenAI API | `OPENAI_API_KEY` |
| `qwen-3.8-flash-next` | Qwen3.8 Flash Next | OpenAI-compatible (vLLM) | a server at `http://127.0.0.1:8000/v1` |
| `haiku-5.5-subagent` | Haiku 5.5 | relay files, answered by Claude Code subagents | a Claude Code session running the relay loop (below) |
| `haiku-5.5-or`, `gpt-6-luna-or`, `qwen-3.8-flash-or` | Haiku 5.5, GPT-6 Luna, Qwen3.8 Flash | OpenRouter | `OPENROUTER_API_KEY` |

Keys come from the environment or from `secrets/credentials.env`; they are never
written to `models.toml`. Change a base URL or backend in `models.local.toml`, which
git ignores:

```toml
[models."qwen-3.8-flash-next"]
base_url = "http://127.0.0.1:8001/v1"
```

To add a model, add a `[models."key"]` entry with `label`, `color`, `backend`, `model`
and, if needed, `base_url` and `key_env`. Optional fields are `max_tokens`,
`thinking_budget` (Anthropic), `effort` (Codex, Claude CLI), `params` (extra request
fields such as `temperature`), `concurrency` and `timeout`.

### OpenRouter

One OpenRouter key runs all three models from any machine:

```bash
export OPENROUTER_API_KEY=sk-or-...        # or put it in secrets/credentials.env
bin/avalon models --ping haiku-5.5-or gpt-6-luna-or qwen-3.8-flash-or
bin/avalon arena --models haiku-5.5-or,gpt-6-luna-or,qwen-3.8-flash-or --games 3 --parallel 3
```

- **The same reasoning effort for every model:** OpenRouter's `reasoning: {effort: "medium"}`.
  Qwen3.8 Flash takes a reasoning token budget rather than an effort level, so OpenRouter
  converts "medium" to about half of `max_tokens` (16k).
- **Qwen3.8 Flash, not Flash Next:** OpenRouter carries Qwen3.8 Flash, the production model
  built on the open-weights Flash Next preview that `bin/serve-qwen` serves. They get separate
  leaderboard rows.
- **Prompt caching:** each move resends the seat's whole history, so input is about 90% of
  the bill.
  - Anthropic and Alibaba models cache only marked parts of the prompt, so `cache = true` marks
    the system prompt and the latest message for Haiku and Qwen. OpenRouter prices Qwen3.8
    Flash's cached input at $0.016 per million tokens, against $0.15 uncached.
  - OpenAI models cache automatically.
  - Every request carries a per-seat `session_id`, so OpenRouter keeps routing a seat to the
    same provider and its cache stays warm.
- **Cost:** OpenRouter reports the cost of every request. The arena logs it per move, and the
  leaderboard shows dollars per seat per game.
- **Errors:**
  - Errors reported inside a successful response (a model failing mid-reply), 429/502/503 and
    other transient errors are retried after 5, 20 and 60 seconds, or longer if the server sends
    `Retry-After`.
  - 401 (bad key), 402 (no credits) and 403 (moderation or budget limit) stop the game at once,
    with OpenRouter's message.
  - A reply cut off at the length limit gets a correction asking for a shorter answer.
- **Reasoning text:** Haiku's and Qwen's reasoning appears in the replay; OpenAI does not return
  Luna's.
- **Reproducibility:** OpenRouter may serve a model from several providers. Pin one with, for
  example, `params = { reasoning = { effort = "medium" }, provider = { order = ["anthropic"],
  allow_fallbacks = false } }` in `models.local.toml`.

### Serving Qwen

Qwen3.8 Flash Next is open weights: 125B parameters, 6B active, plus a 51B n-gram
table. The FP8 checkpoint needs two H200-class GPUs. [bin/serve-qwen](bin/serve-qwen)
has the full setup. Its flags avoid every kernel that would otherwise be compiled at
startup, so no CUDA toolkit is needed.

On a laptop, reach a Qwen server on the GPU box through SSH:

```bash
ssh -N -L 8000:127.0.0.1:8000 <gpu-box>
```

## The three-way match

On a machine where Codex and Claude Code are signed in, one command runs it:

```bash
QWEN_SSH=<gpu-box> bin/three-way
```

`bin/three-way` opens the SSH tunnel to Qwen, if you give it one. It then sends each model
one tiny real request (`bin/avalon models --ping …`), stops with the reason if any model
can't answer, and plays the series. `GAMES`, `PARALLEL`, `PLAYERS` and `MODELS` adjust it,
and `MOCK=1` makes it a free dry run. The same thing by hand:

```bash
bin/avalon models --ping haiku-5.5-cli gpt-6-luna qwen-3.8-flash-next
bin/avalon arena --models haiku-5.5-cli,gpt-6-luna,qwen-3.8-flash-next --players 6 --games 3
```

Both signed-in clients have played whole arena games in tests: Claude Code 2.1.295
(`claude -p`) and the Codex 0.163 app-server, each with the stand-in as its model. To
rerun those tests, set `AVALON_TEST_CLAUDE_BINARY` or `AVALON_TEST_CODEX_BINARY` and run
`tests/test_arena.py`.

`haiku-5.5-cli` and `gpt-6-luna` use your own Claude and ChatGPT sign-ins, so their
moves count against those plans' usage. If `claude` isn't on your PATH, set
`AVALON_CLAUDE_BINARY` to the Claude Code executable.

A 6-player game takes about 10 to 25 minutes, depending on the slowest model at the
table and the discussion length: roughly 25 to 35 turns, with every seat answering
at once. Qwen at medium reasoning effort answers in about 8 to 25 seconds.

Six seats give each model two seats per game. Seats are shuffled for every game and
roles are dealt at random. `--parallel 2` runs games side by side. `--max-rounds`
(default 3) caps the discussion rounds per proposal. `--huddle-rounds` (default 1) sets how
many rounds Evil confers privately before the Assassin names Merlin; with 2 or more, each Evil
player sees the earlier rounds and can answer them. Results print as a leaderboard
and appear in the Arena tab. `bin/avalon leaderboard --tag <tag>` reprints them later.

Models can also share a table with bots in a normal game:

```bash
bin/avalon run --players 7 --seat P1=model:qwen-3.8-flash-next --seat P2=bots/example_bot.py
```

## Subagent players (Haiku through Claude Code)

`haiku-5.5-subagent` lets a Claude Code session play Haiku 5.5 seats with its own subagents
(the Agent tool with `model: "haiku"`). No API key is needed. The referee writes each turn
for a relay seat to `games/.relay/<game>-<seat>-NNN.request.md`: the rules, that seat's secret
role, its whole conversation so far, and THIS TURN. It then waits for the matching `.reply.txt`.

1. Start the game in the background:
   ```bash
   bin/avalon arena --models haiku-5.5-subagent,bots/example_bot.py,scripted --timeout 1800 --max-rounds 2
   ```
2. Repeat until it prints `no game is running`:
   ```bash
   bin/avalon relay --wait 120
   ```
   For each listed request, spawn one Haiku subagent (several at once are fine) with:

   > You are an AI player in a game of The Resistance: Avalon, playing to win for your side.
   > 1. Read the file REQUEST completely (if it is long, keep reading with offsets until the end). It contains the rules, your secret role, your conversation with the referee so far, and THIS TURN at the end.
   > 2. Decide your move for THIS TURN.
   > 3. Use the Write tool to write your reply to REPLY. The reply may start with brief reasoning, and it must end with exactly one JSON object in the format THIS TURN asks for.
   >
   > Fair play: do not open, list, search or write any other file, and do not run shell commands. Other players' files and the referee's files are off limits.
   >
   > When the reply file is written, answer with the single word: done.
3. Afterwards, check fair play from the subagents' own transcripts:
   ```bash
   bin/avalon relay --audit GAME_ID --transcripts ~/.claude/projects/<project>/<session>/subagents
   ```
   It fails if any subagent touched a file other than its own turn's request and reply, and it lists the model each subagent ran on.

Subagents have file tools. Fair play rests on the instruction above, and the audit checks it
after the fact. Each turn is a fresh subagent that reads the seat's whole history, so time
per move (about 10 to 200 seconds) includes subagent start-up.

## How a model plays

- **System prompt, per seat.** The game's rules, the seat's secret role card (only its own night-phase knowledge), and the reply format: end with one JSON object holding the move, a private `reason`, and `beliefs` (probability that each other player is Evil).
- **One message per turn.** What happened since the seat's last turn, written out as text (proposals, every discussion message, votes, quest results), then the decision to make and the turn's facts. The model keeps its whole conversation for the game.
- **Validation and retries.** The shared validator checks every move. An invalid reply gets the errors back and up to two more tries. After that, or if the model cannot be reached or runs out of time, the turn is not published and the game stops. The referee never substitutes a move. Stopped games count against that model in the leaderboard.
- **No tools.** Model players get no tools. The Claude CLI runs with `--tools ""` and none of your settings. Codex runs in a sandbox that can see only an empty scratch folder, with network off. HTTP models just answer.
- **Reasoning.** Each model reasons natively: Haiku with a thinking budget, Luna with Codex reasoning effort, and Qwen in its default thinking mode with Qwen's recommended sampling. Whatever reasoning the API returns appears in the replay.

## What to read into results

Avalon has high variance: hidden roles, small teams and random seating. Compare models
over many games, and look at the Good and Evil win rates separately. Dry runs with
stand-in models are excluded from the leaderboard unless you include them.

## Troubleshooting

- **"cannot seat …: needs …"** comes up before any game files exist. `bin/avalon models` lists what each model is missing. When another backend for the same model is ready, the message names it (for example `haiku-5.5-cli`).
- **The Anthropic API rejects the request** (HTTP 400 that mentions thinking): set `thinking_budget = 0` for that model in `models.local.toml`.
- **"Codex does not offer gpt-6-luna to this login"**: the signed-in Codex catalog lacks the model. The arena never substitutes another model.
- **"cannot reach http://127.0.0.1:8000/v1"**: the Qwen server is down, or the SSH tunnel is closed. The game stops at that turn and keeps its replay.
- **A game stopped mid-way**: the referee publishes a turn only when every seat has answered validly. The error names the seat and model; the leaderboard counts it under "Stopped". Each seat's full exchange with its model is in `games/<id>/players/pN/audit/codex-transcript.jsonl`.
