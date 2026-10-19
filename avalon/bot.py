"""Write an Avalon bot: subclass Bot and override the moves you care about.

    from avalon.bot import Bot

    class MyBot(Bot):
        def vote(self, turn, state):
            return {"vote": "approve", "reason": "I trust everyone."}

    bin/avalon run --players 5 --seat P1=bots/my_bot.py

The base class plays every turn with a random legal move, so any subclass is
a complete player from the start. Every method receives:

  turn   this turn's facts for you: kind, quest, proposal, leader, team_size,
         team, round, can_amend, options, candidates, evil_team, ... (only
         the keys that apply to this kind of turn; see CONTRIBUTING.md)
  state  the public record: score, quests, vote_history, leader_order, ...
         plus "events", every public event so far (proposals, discussion
         messages, votes, quest results)

A move is a dict. Leave out "turn" and "beliefs"; the adapter adds them (the
beliefs come from your beliefs() method). An optional "reason" string is
kept private: it goes into your decision notes, which the referee's replay
shows. Bots run inside the referee process, unsandboxed; read only what
these arguments give you.
"""

from __future__ import annotations

import random


class Bot:
    name: str | None = None          # label in summaries and stats; defaults to the class name

    def __init__(self, player: str, knowledge: dict, players: list[str], rng: random.Random):
        self.player = player         # your seat, e.g. "P3"
        self.knowledge = knowledge   # your night-phase knowledge (see CONTRIBUTING.md)
        self.role = knowledge["role"]
        self.side = knowledge["side"]
        self.players = players       # every seat, in order
        self.rng = rng               # seeded per game when --seed is given; use it for reproducible games
        self.setup()

    def setup(self):
        """Called once before the first turn. Override to set up your own state."""

    def act(self, turn: dict, state: dict) -> dict:
        """Dispatch to the method named after the turn kind. Override to handle every kind yourself."""
        return getattr(self, turn["kind"])(turn, state)

    # -- one method per turn kind ------------------------------------------------
    def onboard(self, turn, state):
        return {"ack": True}

    def propose(self, turn, state):
        others = [p for p in self.players if p != self.player]
        return {"team": [self.player] + self.rng.sample(others, turn["team_size"] - 1), "message": None}

    def discuss(self, turn, state):
        return {"message": None, "ready_to_vote": True}

    def vote(self, turn, state):
        return {"vote": self.rng.choice(["approve", "reject"])}

    def quest(self, turn, state):
        return {"card": self.rng.choice(turn["options"])}

    def evil_huddle(self, turn, state):
        return {"message": None, "suggested_target": None}

    def assassinate(self, turn, state):
        return {"target": self.rng.choice(turn["candidates"]), "message": None}

    def reflect(self, turn, state):
        return {"message": None}

    # -- private state the replay shows ------------------------------------------------
    def beliefs(self, turn, state) -> dict[str, float]:
        """Your probability (0 to 1) that each other player is Evil.

        The default uses only your night-phase knowledge: players you know
        are Evil get 1.0 and the remaining Evil seats are spread evenly over
        everyone else.
        """
        others = [p for p in self.players if p != self.player]
        known = set(self.knowledge.get("sees_evil", [])) | set(self.knowledge.get("evil_teammates", []))
        unknown = [p for p in others if p not in known]
        hidden = turn["evil_total"] - len(known) - (self.side == "evil")
        prior = min(1.0, max(0.0, hidden / len(unknown))) if unknown else 0.0
        return {p: 1.0 if p in known else round(prior, 3) for p in others}

    def memory(self) -> str | None:
        """Optional text saved as your private memory after each turn (the replay shows it)."""
        return None
