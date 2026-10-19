# Prompt tournament

Eight teams each enter a prompt. Every game seats all eight teams once, each team's player is the same model
(Haiku 5.5 through OpenRouter by default) running that team's prompt, and the series rotates roles so every
team plays every role equally often. Spectators follow along on a website.

## The setup

Always 8 players: 5 Good (Merlin, Percival, 3 Loyal Servants) against 3 Evil (Morgana, the Assassin, Oberon). Merlin sees all three Evil players. Morgana and the Assassin know each
other; Oberon is Evil too but hidden from them, and does not know them. Percival sees Merlin and Morgana
without knowing which is which. Quests take 3, 4, 4, 5, 5 players; quest 4 needs 2 fail cards.

## What a team submits

```
teams/<team-id>/player.md     the team's system prompt, at most 1,500 words
teams/<team-id>/team.toml     optional: name = "Display name", color = "#3b82f6"
```

The folder name is the team id (lowercase letters, digits, `-`, `_`). Check a submission before sending it:

```bash
bin/avalon teams check teams/my-team
```

This checks the format and the word cap, then plays one free game with the team in every seat against a
stand-in model (random legal moves), to show the prompt plugs into the harness. `--no-game` skips the game.

Start from one of the baselines in [teams/_baselines](teams/_baselines): `minimal`, `deducer` (structured
memory and belief updates), `cautious-merlin` (protecting Merlin) and `coordinated-evil` (Evil conventions).
Each explains this setup and the basic strategy.

## How a player works

There is no conversation. Every turn is one request:

- **System** (the same all game, so the provider caches it): the rules, how turns work, the seat's secret
  role card, and the team's prompt.
- **User**: the seat's memory, the public events since its last turn, the board (score, finished quests,
  rejections, team sizes, leader order), the decision, the turn facts and the reply format.

The reply ends with one JSON object: the move, `reason` (private), `beliefs` (probability each other player is
Evil) and `memory`: the full rewritten memory, at most 800 words, or `null` to keep it. Whatever the memory
leaves out is gone next turn, so the team prompt should say what to keep in it. Over the cap is an invalid
reply.

Other players' words always appear as JSON strings after their seat (`P3: "..."`), so no player can pose as
the referee. The harness rules take precedence over team prompts.

**Faults.** An invalid reply gets the errors back and two more tries. After that the referee plays a safe
default move (pass, approve, play success, or propose yourself and the next seats), records a fault for the
team, and the game goes on. A provider that stays unreachable through its retries, or a turn that times out,
gets the same default move but counts as a provider error, not a team fault. A bad key or no credits stops
the game.

## Running the tournament

```bash
export OPENROUTER_API_KEY=sk-or-...
bin/avalon teams check teams/
bin/avalon tournament --teams teams/ --games 16 --model haiku-5.5-or --parallel 2 --seed 2026 --tag final
bin/avalon standings --tag final
```

- **Exactly 8 teams.** With fewer entries, fill the field with baseline copies: `bin/avalon teams demo DIR --count N`.
- **Balanced roles.** Each block of 8 games is a shuffled Latin square: each team holds each of the 8 role
  slots once, so 16 games give every team every role twice (10 Good games, 6 Evil). Seats are shuffled every
  game. `--seed` makes the schedule reproducible; it is saved with the game ids in
  `games/.<tag>.tournament.json`. Players have no memory across games, so the schedule gives nothing away.
- **Dry run first:** `--mock` plays the whole series against the stand-in model for free, in seconds.
- **Parallel games:** each game sends up to 8 requests at once. `haiku-5.5-or` allows 16 concurrent requests,
  enough for `--parallel 2`; for more, raise it in `models.local.toml`: `[models."haiku-5.5-or"]` `concurrency = 32`.
- **Cost and time:** OpenRouter's cost is logged per move and shown per team in `standings`. Run one real game
  first (`--games 1`) to measure.

**Scoring.** Score = the mean of a team's Good win rate and Evil win rate, so both sides count equally. Ties
go to Evil read (as a Good player other than Merlin: average final P(Evil) given to real Evil players minus
that given to Good players), then Merlin survival, then Assassin accuracy, then fewer faults. Stopped games do
not count.

## Two prompts head to head

`bin/avalon versus teams/a teams/b --games 16` plays pairs of games on one deal: A holds every Good seat and
B every Evil seat, then they swap. It reports how often each prompt wins, with a 95% interval. See
[the README](README.md#compare-two-solutions-head-to-head).

## Trajectories

```bash
bin/avalon trajectories GAME_ID ... --out baseline.jsonl          # every seat
bin/avalon trajectories GAME_ID ... --team my-team --out mine.jsonl
```

One JSON record per seat-turn: the input sections (harness, role card, team prompt, memory, events, board,
decision, facts, reply schema), memory before and after, the move, reason, model reasoning, token usage and
cost, rejected attempts, any fault, and the game's outcome for that seat. Hand teams the trajectories of
baseline games to improve from. Trajectories of real games contain the team prompt of every seat they
include: use `--team` to give each team only its own.

## Hosting

Prompts stay secret until the final, and nobody should see roles or private notes while a game runs.

### Live: the dashboard in public mode

```bash
bin/avalon dashboard --public --tag final --reveal-prompts 2026-11-30T18:00
```

Public mode serves only tournament games (only the `--tag` series if given) and:

- while a game runs: the public record only (proposals, discussion, votes, quest results). No roles, notes,
  memory, beliefs, reasoning, Evil huddle or prompts;
- once a game ends: roles, private notes, memory, beliefs and reasoning, so spectators can see how each
  player thought. The model's notes can paraphrase its team prompt;
- never: terminals, transcripts, logs, process facts or the match launcher;
- team prompts only after `--reveal-prompts` (`never` by default, `now`, or an ISO time, UTC unless given);
- the Arena tab shows standings.

It binds to 127.0.0.1. Expose it through one of these:

**Cloudflare Tunnel** (no open ports, free HTTPS):

```bash
cloudflared tunnel --url http://127.0.0.1:8777          # quick tunnel: prints a trycloudflare.com URL
# or a named tunnel on your own domain:
cloudflared tunnel create avalon
cloudflared tunnel route dns avalon avalon.example.org
cloudflared tunnel run --url http://127.0.0.1:8777 avalon
```

**Caddy** on a server with a domain pointing at it (automatic HTTPS):

```
avalon.example.org {
    reverse_proxy 127.0.0.1:8777
}
```

Run the private dashboard (`bin/avalon dashboard`) only on localhost or through an SSH tunnel: it shows
everything.

### Static: GitHub Pages or Cloudflare Pages

```bash
bin/avalon export-site --tournament final --out site            # prompts stay private
bin/avalon export-site --tournament final --out site --reveal-prompts   # after the final
```

The folder holds `index.html` (standings and the list of games), `replay.html` (the replay viewer; open a
game with `replay.html?game=ID`), `data/<game>.json` (the same public bundles as the live public mode),
`standings.json` and the character art. Re-export after each round. Export only between games, or finished
games only: a running game is exported as its public record.

- **GitHub Pages:** push the folder to a repository (for example a `gh-pages` branch) and enable Pages for
  that branch in Settings → Pages.
- **Cloudflare Pages:** `npx wrangler pages deploy site --project-name avalon-tournament`, or connect the
  repository with `site` as the output folder.
