"""Every text a player agent reads: public rules, private onboarding, turn files."""

from __future__ import annotations

import json

from .roles import EVIL, GOOD, ROLES, roles_in_play_summary

WORD_LIMIT = 300


def _names(ps):
    return ", ".join(ps) if ps else "(none)"


def rules_md(players, role_keys, sizes, n_fail2, max_rounds, huddle_rounds) -> str:
    rip = roles_in_play_summary(role_keys)
    size_rows = "\n".join(
        f"| {i + 1} | {s} | {2 if (i == 3 and n_fail2) else 1} |" for i, s in enumerate(sizes))
    return f"""# The Resistance: Avalon - rules for this game

Players: {_names(players)} ({len(players)} players)
Good roles in play: {", ".join(rip[GOOD])}
Evil roles in play: {", ".join(rip[EVIL])}

## Goal
Good wins by completing 3 quests (and surviving the Assassin, see below).
Evil wins by failing 3 quests, by getting 5 team proposals rejected in a row
within a single quest, or by the Assassin naming Merlin at the end.

## Quests
| Quest | Team size | Fail cards needed to fail it |
|---|---|---|
{size_rows}

## How a quest is decided
1. **Proposal.** The leader proposes a team of the required size (the leader may
   include themself) and may add a public pitch.
2. **Discussion** (at most {max_rounds} rounds). In each round *every* player submits
   at the same time: one public message (at most {WORD_LIMIT} words) or a pass, plus
   whether they are ready to vote. Nobody sees anyone else's message for a round
   until the round closes, then all of them are published together. The leader may
   change the proposed team during discussion. Discussion ends when everyone is ready
   to vote, when a whole round passes in silence, or after round {max_rounds}.
3. **Vote.** Everyone votes approve or reject at the same time. Votes are public.
   A strict majority of approvals sends the team on the quest; otherwise leadership
   passes to the next player in order and a new proposal begins.
4. **Quest.** Each team member secretly plays a card. Good players must play
   SUCCESS. Evil players may play SUCCESS or FAIL. Only the number of FAIL cards is
   revealed - never who played them.

## Assassination
If Good completes 3 quests, the Evil players confer privately{"" if huddle_rounds else " (no huddle in this game)"}
and the Assassin names one player as Merlin. If that player is Merlin, Evil wins.

## Who knows what at the start
- Merlin sees the Evil players, except Mordred (if in play).
- Percival sees Merlin and Morgana (if in play) as two candidates, without knowing which is which.
- Evil players see each other, except Oberon (if in play). Oberon sees no one and no one
  sees Oberon (Merlin does see Oberon).
- Loyal Servants know nothing.

## How this game is run
- A referee runs the game. You only act when the referee opens a turn for you.
- This folder (the public repo) is the single shared source of truth. It is the same
  for every player and only the referee writes to it, once per turn, after all
  submissions for that turn are in.
- Each player works in a private folder no other player can read. Every turn you must
  write a private decision document explaining your reasoning, plus a submission.
- There is no other way to communicate. Messages are limited to {WORD_LIMIT} words.

## Files here
- `state.json` - current phase, leader, team, score, vote history.
- `updates/NNNN.md` - what happened at turn NNNN (one file per turn).
- `log.md` - the complete public history.
- `events.jsonl` - the same history as machine-readable events.
"""


def role_card(player, info, role_keys) -> str:
    role = ROLES[info["role"]]
    side = "Good (Loyal Servants of Arthur)" if role.side == GOOD else "Evil (Minions of Mordred)"
    lines = [f"# Your secret role: {role.name}", "", f"You are **{player}**. Your side: **{side}**.", "",
             role.blurb, "", "## What you know"]
    if info["role"] == "merlin":
        lines.append(f"- These players are Evil: **{_names(info['sees_evil'])}**.")
        if info["hidden_from_you"]:
            lines.append("- Mordred is in play and is hidden from you: one Evil player is not on that list.")
    elif info["role"] == "percival":
        c = info["merlin_candidates"]
        if info["morgana_in_play"] and len(c) == 2:
            lines.append(f"- One of **{c[0]}** and **{c[1]}** is Merlin; the other is Morgana. You do not know which.")
        else:
            lines.append(f"- Merlin is **{_names(c)}**.")
    elif role.side == EVIL and info["role"] != "oberon":
        lines.append(f"- Your Evil teammates: **{_names(info['evil_teammates'])}**.")
        if info.get("oberon_in_play"):
            lines.append("- Oberon is also Evil but hidden from you (and does not know you).")
    elif info["role"] == "oberon":
        lines.append("- You do not know who your Evil teammates are, and they do not know you.")
    else:
        lines.append("- Nothing beyond your own loyalty. Everyone else is a mystery.")
    lines += ["", "## Your win condition"]
    if role.side == GOOD:
        lines.append("Help Good pass 3 quests: keep Evil players off quest teams. "
                     + ("Then keep Merlin hidden from the Assassin." if "assassin" in role_keys else ""))
    else:
        lines.append("Get 3 quests failed (play FAIL when it helps), or get 5 proposals rejected in one quest"
                     + (", or have the Assassin find Merlin at the end." if "merlin" in role_keys else "."))
    lines += ["", "Nobody else can read this file. You may claim anything in public - lying is part of the game."]
    return "\n".join(lines) + "\n"


def claude_md(player, role_name, side, work, public, n_players=None, n_evil=None) -> str:
    base = (f"There are {n_evil} Evil players among the {n_players} seats, so among the other "
            f"{n_players - 1} players there are {n_evil if side == 'good' else n_evil - 1} Evil players besides you"
            f"{'' if side == 'good' else ' (you are one of the Evil players)'}." if n_players and n_evil else "")
    return f"""# Avalon player {player}

You are **{player}**, an autonomous player in a game of The Resistance: Avalon run by a referee.
Your secret role is **{role_name}** ({side}). Details: `ROLE.md`. Rules: `{public}/RULES.md`.

## Your folders
- Private (only you): `{work}`
  - `ROLE.md` - your secret role and night-phase knowledge.
  - `memory.md` - **your long-term memory. Read it at the start of every turn and update it before you submit.**
  - `inbox/` - turn instructions from the referee. Read-only.
  - `notes/` - your private decision documents (required every turn).
  - `outbox/` - your submissions.
  - `scratch/` - anything else you want to keep.
- Public game repo (read-only, identical for all players): `{public}`

## Every turn
1. Read the turn file named in the referee's message (`inbox/turn_NNNN.md`) and your `memory.md`.
2. Read the public files the turn file lists (always `state.json` and the new `updates/`).
3. Update your beliefs like a Bayesian (below). Decide.
4. Update `memory.md` (keep it organised, under ~800 words): certain facts; the record of every
   proposal, vote and quest result so far; claims players made and any contradictions; your current
   probability for each player with the reason; your plan. Your conversation context can be
   compacted during a long game - `memory.md` is what you will rely on.
5. Write your private decision document to the notes path given: prior -> evidence -> posterior for
   the players whose probability changed, and why you chose this action. Be candid; other players
   never see it (post-game auditors do).
6. Write your submission JSON to the outbox path given, exactly in the shown shape, including your
   private `beliefs` (probability that each other player is Evil).
7. Stop. Do not wait or poll. The referee collects every submission at the same time and publishes
   the results to the public repo.

## Reason like a Bayesian
- {base} Start every player you know nothing about at that base rate. Your probabilities over the
  other players should add up to about the number of Evil players you have not identified for sure.
- Treat every event as evidence. Ask: how much more likely is this if the player is Evil than if they
  are Good (the likelihood ratio)? Multiply the odds by it; never jump straight to 0 or 1 without proof.
- Rules of thumb: a failed quest with F fail cards means at least F of its members are Evil - raise
  each member's odds sharply (more for small teams, and exclude yourself if you are Good). A clean
  success lowers members' odds only somewhat (Evil sometimes plays success to look trustworthy).
  Approving a team that then failed is mild evidence; rejecting a clean team for no stated reason is mild
  evidence; contradictions between someone's words and their votes are real evidence.
- Your role card is certainty. Use it, but do not reveal more than your side can afford.

## Arguing in discussion
- Make a real argument, not a slogan: cite concrete evidence (quest results, who voted how,
  inconsistencies), state your current reads and how confident you are, answer the points other
  players made in the previous round, and propose a concrete team or vote.
- Aim for 100-250 words when you have something to say; the hard limit is 300. Pass (null) only when
  you truly have nothing to add.

## Conduct
- You can only act through your submission files. You cannot reach other players.
- Do not try to read anything outside your private folder and the public repo: access is blocked
  and logged.
- Play to win for your side.
"""


def onboard_prompt(work, instructions_name="CLAUDE.md") -> str:
    return (f"Welcome to Avalon. Read {work}/{instructions_name} and {work}/ROLE.md, then open "
            f"{work}/inbox/turn_0001.md and follow it.")


def turn_prompt(spec) -> str:
    label = {"onboard": "onboarding", "propose": "you are the leader", "discuss": f"discussion round {spec.get('round')}",
             "vote": "team vote", "quest": "quest card", "evil_huddle": "evil huddle",
             "assassinate": "assassination", "reflect": "post-game reflection"}[spec["kind"]]
    return (f"Turn {spec['turn']:04d} is open ({label}). Read {spec['inbox_path']} and follow it.")


def nudge_prompt(spec, secs_left) -> str:
    return (f"Reminder: turn {spec['turn']:04d} closes in about {secs_left} seconds. Write {spec['notes_path']} "
            f"and {spec['submit_path']} now, then stop.")


BELIEF_KINDS = ("propose", "discuss", "vote", "quest", "evil_huddle", "assassinate")


def _example(spec) -> dict:
    ex = _example_core(spec)
    if spec["kind"] in BELIEF_KINDS:
        others = [p for p in spec["players"] if p != spec["you"]]
        ex["beliefs"] = {p: 0.5 for p in others}
        ex["role_guesses"] = {others[0]: "Merlin?"}
    return ex


def _example_core(spec) -> dict:
    t = spec["turn"]
    k = spec["kind"]
    if k == "onboard":
        return {"turn": t, "ack": True}
    if k == "propose":
        return {"turn": t, "team": spec["players"][:spec["team_size"]], "message": "Short public pitch, or null."}
    if k == "discuss":
        ex = {"turn": t, "message": "Your public message (max 300 words), or null to pass.", "ready_to_vote": False}
        if spec.get("can_amend"):
            ex["team"] = None
        return ex
    if k == "vote":
        return {"turn": t, "vote": "approve or reject"}
    if k == "quest":
        return {"turn": t, "card": " or ".join(spec["options"])}
    if k == "evil_huddle":
        return {"turn": t, "message": "Private message to the Evil team (max 300 words).", "suggested_target": "P?"}
    if k == "assassinate":
        return {"turn": t, "target": "P?", "message": "Optional public last words, or null."}
    return {"turn": t, "message": "Optional public post-game message (max 300 words), or null."}


def turn_md(spec: dict, public_ctx: dict, private_ctx: list[str]) -> str:
    k = spec["kind"]
    head = (f"# Turn {spec['turn']:04d} - {k.replace('_', ' ')}"
            f" - Quest {spec['quest']}, proposal {spec['proposal']}"
            + (f", round {spec['round']}/{spec['max_rounds']}" if k == "discuss" else ""))
    sc = public_ctx["score"]
    status = [f"You are **{spec['you']}**. Leader: **{spec['leader']}**. "
              f"Score: Good {sc['good']} - Evil {sc['evil']}. "
              f"Rejected proposals this quest: {public_ctx['rejected_proposals_this_quest']}/5."]
    if spec.get("team"):
        status.append(f"Proposed team ({len(spec['team'])}): **{', '.join(spec['team'])}**.")
    reads = "\n".join(f"- `{p}`" for p in spec["read_first"])
    body = {
        "onboard": lambda: (
            f"Welcome. Read `{spec.get('instructions_name', 'CLAUDE.md')}` and `ROLE.md` in your private folder, then the public `RULES.md`.\n"
            "Write your opening strategy as your private decision document (who you are, what you know, "
            "how you intend to play), then acknowledge."),
        "propose": lambda: (
            f"You are the leader for quest {spec['quest']}. Choose exactly **{spec['team_size']}** players "
            "(you may include yourself). You may add a short public pitch in `message`. "
            "Everyone will discuss your proposal, then vote."),
        "discuss": lambda: (
            f"Discussion round {spec['round']} of at most {spec['max_rounds']}. Post one public message "
            f"(aim for 100-250 words, hard limit {WORD_LIMIT}) or pass with `null`. Argue from evidence: "
            "quest results, the vote record, what each player said versus how they voted; answer the points made "
            "in the previous round by name; say who you would put on or keep off this team and why. "
            "Set `ready_to_vote` to true when you have nothing more to add. Discussion ends when everyone is "
            "ready, when nobody speaks for a round, or after the last round. All messages of this round are "
            "published together when it closes."
            + ("\n\nYou are the leader: you may change the proposed team by setting `team` to a new list of "
               f"exactly {spec['team_size']} players (otherwise leave it null)." if spec.get("can_amend") else "")),
        "vote": lambda: (
            f"Vote on the team **{', '.join(spec.get('team', []))}**. A strict majority of approvals sends it on "
            "the quest. Votes are revealed publicly. "
            + ("**This is proposal 5 of this quest: if it is rejected, Evil wins immediately.**"
               if spec.get("rejections_so_far", 0) >= 4 else
               "If 5 proposals in a row are rejected in one quest, Evil wins.")),
        "quest": lambda: (
            "You are on the quest team. Play one card in secret; only the number of FAIL cards is revealed. "
            f"Your options: **{', '.join(spec['options'])}**. "
            f"This quest fails with {spec['fails_required']} FAIL card(s)."),
        "evil_huddle": lambda: (
            "Good has completed three quests. This is Evil's last chance: the Assassin names one player as "
            f"Merlin. The Evil team is **{', '.join(spec['evil_team'])}**; the Assassin is "
            f"**{spec['assassin']}**. Only Evil players see this huddle. Share who you think Merlin is and why, "
            f"and suggest a target from: {', '.join(spec['candidates'])}."),
        "assassinate": lambda: (
            f"You are the Assassin. Name the player you believe is Merlin, from: "
            f"{', '.join(spec['candidates'])}. If you are right, Evil wins."),
        "reflect": lambda: (
            "The game is over. Final roles are in the update file. In your private decision document, "
            "reflect honestly: what you believed about each player, which signals were right or wrong, and "
            "what you would do differently. Optionally post a short public message."),
    }[k]()
    priv = ""
    if private_ctx:
        priv = "\n## Private information for you\n" + "\n".join(private_ctx) + "\n"
    example = json.dumps(_example(spec), indent=2)
    beliefs_help = ("`beliefs` (required, private, never published): for **every** other player, your current "
                    "probability from 0 to 1 that they are Evil, updated like a Bayesian from your last values "
                    f"(base rate: {spec.get('evil_total', '?')} Evil players in this game). Use what you know for "
                    "certain (your role card) and update on votes, quest results and arguments. `role_guesses` (optional, private): who you think "
                    "holds a special role, e.g. {\"P3\": \"Merlin\"}.\n" if k in BELIEF_KINDS else "")
    machine = {key: spec[key] for key in spec if key not in ("inbox_path",)}
    return f"""{head}

{" ".join(status)}

## 1. Read the public repo first
{reads}
(Full history: `{spec['public_dir']}/log.md`. Rules: `{spec['public_dir']}/RULES.md`.)
{priv}
## 2. Your decision
{body}

## 3. Update your memory -> `{spec.get('memory_path', 'memory.md')}`
Add what happened since your last turn (proposals, votes, quest results, claims) and your updated reads.
Required every turn; the referee will not accept the turn otherwise.

## 4. Private decision document -> `{spec['notes_path']}`
At least 20 words: prior -> evidence -> posterior for the players whose probability changed, and why you
chose this action. No other player can read it.

## 5. Submission -> `{spec['submit_path']}`
```
{example}
```
{beliefs_help}
## 6. Stop
End your turn once all three files are written. Do not wait for other players.

## Machine-readable spec
```json
{json.dumps(machine)}
```
"""
