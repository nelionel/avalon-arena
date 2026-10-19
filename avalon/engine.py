"""Pure Avalon game state machine (no I/O).

The referee asks `actors()` who must act now, builds a turn for them, collects
all submissions at once, then calls `apply(actions)`. `apply` returns events,
each tagged with who may see it:

    "public"   -> published to the public repo
    "evil"     -> delivered only to evil players' inboxes (assassination huddle)
    "referee"  -> referee-only (e.g. who played which quest card)
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .roles import EVIL, GOOD, ROLES, TEAM_SIZES, fails_required

ONBOARD, PROPOSE, DISCUSS, VOTE, QUEST = "onboard", "propose", "discuss", "vote", "quest"
HUDDLE, ASSASSINATE, REFLECT, OVER = "evil_huddle", "assassinate", "reflect", "over"


@dataclass
class Proposal:
    quest: int
    number: int
    leader: str
    team: list[str]
    pitch: str | None = None
    rounds: list[list[dict]] = field(default_factory=list)
    amendments: list[dict] = field(default_factory=list)
    votes: dict[str, str] = field(default_factory=dict)
    approved: bool | None = None


@dataclass
class QuestResult:
    quest: int
    team: list[str]
    fails: int
    fails_required: int
    success: bool


class Game:
    def __init__(self, assignment: dict[str, str], *, first_leader: str | None = None,
                 max_rounds: int = 10, huddle_rounds: int = 1, reflect: bool = True,
                 rng: random.Random | None = None):
        self.assignment = dict(assignment)
        self.players = sorted(assignment, key=lambda p: int(p[1:]))
        self.n = len(self.players)
        self.sizes = TEAM_SIZES[self.n]
        self.max_rounds = max_rounds
        self.huddle_rounds = huddle_rounds
        self.reflect_enabled = reflect
        rng = rng or random.SystemRandom()
        self.leader_idx = self.players.index(first_leader) if first_leader else rng.randrange(self.n)
        self.phase = ONBOARD
        self.quest_idx = 0
        self.proposal_no = 1          # within the current quest, 1..5
        self.proposals: list[Proposal] = []
        self.results: list[QuestResult] = []
        self.round = 0
        self.huddle_round = 0
        self.huddle_log: list[dict] = []
        self.winner: str | None = None
        self.win_reason: str | None = None
        self.assassin_target: str | None = None

    # -- queries -----------------------------------------------------------
    @property
    def leader(self) -> str:
        return self.players[self.leader_idx % self.n]

    @property
    def current(self) -> Proposal | None:
        return self.proposals[-1] if self.proposals else None

    def side(self, p: str) -> str:
        return ROLES[self.assignment[p]].side

    def role_holder(self, role: str) -> str | None:
        return next((p for p, r in self.assignment.items() if r == role), None)

    @property
    def evil_players(self) -> list[str]:
        return [p for p in self.players if self.side(p) == EVIL]

    @property
    def score(self) -> dict[str, int]:
        return {GOOD: sum(r.success for r in self.results), EVIL: sum(not r.success for r in self.results)}

    def team_size(self) -> int:
        return self.sizes[self.quest_idx]

    def fails_needed(self) -> int:
        return fails_required(self.n, self.quest_idx)

    def actors(self) -> list[str]:
        if self.phase in (ONBOARD, DISCUSS, VOTE, REFLECT):
            return list(self.players)
        if self.phase == PROPOSE:
            return [self.leader]
        if self.phase == QUEST:
            return list(self.current.team)
        if self.phase == HUDDLE:
            return list(self.evil_players)
        if self.phase == ASSASSINATE:
            return [self.role_holder("assassin")]
        return []

    def turn_fields(self, p: str) -> dict:
        """Kind-specific, per-player fields for the turn spec."""
        f: dict = {"kind": self.phase, "quest": self.quest_idx + 1, "proposal": self.proposal_no,
                   "leader": self.leader, "team_size": self.team_size(), "fails_required": self.fails_needed()}
        if self.phase in (DISCUSS, VOTE, QUEST) and self.current:
            f["team"] = list(self.current.team)
        if self.phase == DISCUSS:
            f["round"] = self.round
            f["max_rounds"] = self.max_rounds
            f["can_amend"] = p == self.leader
        if self.phase == VOTE:
            f["rejections_so_far"] = self.proposal_no - 1
        if self.phase == QUEST:
            f["options"] = ["success", "fail"] if self.side(p) == EVIL else ["success"]
        if self.phase in (HUDDLE, ASSASSINATE):
            f["candidates"] = [q for q in self.players if self.side(q) == GOOD]
            f["assassin"] = self.role_holder("assassin")
            f["evil_team"] = list(self.evil_players)
            f["huddle_round"] = self.huddle_round + 1
            f["huddle_rounds"] = self.huddle_rounds
        if self.phase == REFLECT:
            f["roles"] = {q: ROLES[r].name for q, r in self.assignment.items()}
            f["winner"] = self.winner
            f["win_reason"] = self.win_reason
        return f

    # -- transitions -------------------------------------------------------
    def apply(self, actions: dict[str, dict]) -> list[dict]:
        missing = set(self.actors()) - set(actions)
        if missing:
            raise ValueError(f"missing actions from {sorted(missing)}")
        handler = {ONBOARD: self._onboard, PROPOSE: self._propose, DISCUSS: self._discuss,
                   VOTE: self._vote, QUEST: self._quest, HUDDLE: self._huddle,
                   ASSASSINATE: self._assassinate, REFLECT: self._reflect}[self.phase]
        return handler(actions)

    def _onboard(self, actions):
        self.phase = PROPOSE
        return [{"type": "game_started", "visibility": "public", "leader": self.leader,
                 "quest": 1, "team_size": self.team_size()}]

    def _propose(self, actions):
        a = actions[self.leader]
        prop = Proposal(self.quest_idx + 1, self.proposal_no, self.leader, list(a["team"]), a.get("message"))
        self.proposals.append(prop)
        self.phase, self.round = DISCUSS, 1
        return [{"type": "proposal", "visibility": "public", "quest": prop.quest, "proposal": prop.number,
                 "leader": prop.leader, "team": prop.team, "message": prop.pitch}]

    def _discuss(self, actions):
        prop = self.current
        entries = [{"player": p, "message": actions[p].get("message"),
                    "ready": bool(actions[p].get("ready_to_vote"))} for p in self.players]
        prop.rounds.append(entries)
        events = [{"type": "discussion", "visibility": "public", "quest": prop.quest,
                   "proposal": prop.number, "round": self.round, "entries": entries}]
        amended = False
        new_team = actions[self.leader].get("team")
        if new_team and sorted(new_team) != sorted(prop.team):
            prop.amendments.append({"round": self.round, "from": prop.team, "to": list(new_team)})
            prop.team = list(new_team)
            amended = True
            events.append({"type": "team_amended", "visibility": "public", "quest": prop.quest,
                           "proposal": prop.number, "round": self.round, "leader": self.leader,
                           "team": prop.team})
        # An amendment always earns another round (unless the cap is hit).
        reason = None
        if not amended:
            if all(e["ready"] for e in entries):
                reason = "all_ready"
            elif not any(e["message"] for e in entries):
                reason = "silence"
        if reason is None and self.round >= self.max_rounds:
            reason = "max_rounds"
        if reason:
            events.append({"type": "discussion_closed", "visibility": "public", "quest": prop.quest,
                           "proposal": prop.number, "rounds": self.round, "reason": reason})
            self.phase = VOTE
        else:
            self.round += 1
        return events

    def _vote(self, actions):
        prop = self.current
        prop.votes = {p: actions[p]["vote"] for p in self.players}
        approvals = sum(v == "approve" for v in prop.votes.values())
        prop.approved = approvals * 2 > self.n
        events = [{"type": "vote_result", "visibility": "public", "quest": prop.quest,
                   "proposal": prop.number, "leader": prop.leader, "team": prop.team,
                   "votes": prop.votes, "approvals": approvals, "approved": prop.approved}]
        if prop.approved:
            self.phase = QUEST
            return events
        if self.proposal_no >= 5:
            return events + self._end(EVIL, "five_rejections")
        self.proposal_no += 1
        self.leader_idx += 1
        self.phase = PROPOSE
        return events

    def _quest(self, actions):
        prop = self.current
        cards = {p: actions[p]["card"] for p in prop.team}
        for p, c in cards.items():  # the referee already enforces this; keep the engine honest too
            if c == "fail" and self.side(p) != EVIL:
                raise ValueError(f"{p} is good and cannot fail a quest")
        fails = sum(c == "fail" for c in cards.values())
        res = QuestResult(self.quest_idx + 1, list(prop.team), fails, self.fails_needed(), fails < self.fails_needed())
        self.results.append(res)
        events = [
            {"type": "quest_cards", "visibility": "referee", "quest": res.quest, "cards": cards},
            {"type": "quest_result", "visibility": "public", "quest": res.quest, "team": res.team,
             "fails": fails, "successes": len(prop.team) - fails, "fails_required": res.fails_required,
             "success": res.success, "score": self.score},
        ]
        sc = self.score
        if sc[EVIL] >= 3:
            return events + self._end(EVIL, "three_failed_quests")
        if sc[GOOD] >= 3:
            if self.role_holder("assassin") and self.role_holder("merlin"):
                self.phase = HUDDLE if self.huddle_rounds > 0 else ASSASSINATE
                events.append({"type": "assassination_phase", "visibility": "public",
                               "note": "Good completed three quests. Evil may now try to find Merlin."})
                return events
            return events + self._end(GOOD, "three_successful_quests")
        self.quest_idx += 1
        self.proposal_no = 1
        self.leader_idx += 1
        self.phase = PROPOSE
        return events

    def _huddle(self, actions):
        self.huddle_round += 1
        entries = [{"player": p, "message": actions[p].get("message"),
                    "suggested_target": actions[p].get("suggested_target")} for p in self.evil_players]
        self.huddle_log.append({"round": self.huddle_round, "entries": entries})
        if self.huddle_round >= self.huddle_rounds:
            self.phase = ASSASSINATE
        return [{"type": "evil_huddle", "visibility": "evil", "round": self.huddle_round, "entries": entries}]

    def _assassinate(self, actions):
        assassin = self.role_holder("assassin")
        a = actions[assassin]
        self.assassin_target = a["target"]
        hit = self.assignment.get(a["target"]) == "merlin"
        events = [{"type": "assassination", "visibility": "public", "assassin": assassin,
                   "target": a["target"], "message": a.get("message"), "hit": hit}]
        if hit:
            return events + self._end(EVIL, "merlin_assassinated")
        return events + self._end(GOOD, "merlin_survived")

    def _end(self, winner, reason):
        self.winner, self.win_reason = winner, reason
        self.phase = REFLECT if self.reflect_enabled else OVER
        return [{"type": "game_over", "visibility": "public", "winner": winner, "reason": reason,
                 "roles": {p: ROLES[r].name for p, r in self.assignment.items()},
                 "score": self.score}]

    def _reflect(self, actions):
        self.phase = OVER
        return [{"type": "reflections", "visibility": "public",
                 "entries": [{"player": p, "message": actions[p].get("message")} for p in self.players]}]

    # -- snapshots -----------------------------------------------------------
    def public_state(self) -> dict:
        prop = self.current
        return {
            "phase": self.phase,
            "quest": self.quest_idx + 1,
            "proposal": self.proposal_no,
            "leader": self.leader,
            "leader_order": self.players[self.leader_idx % self.n:] + self.players[:self.leader_idx % self.n],
            "team_size": self.team_size(),
            "fails_required": self.fails_needed(),
            "discussion_round": self.round if self.phase == DISCUSS else None,
            "max_discussion_rounds": self.max_rounds,
            "proposed_team": prop.team if prop and self.phase in (DISCUSS, VOTE, QUEST) else None,
            "rejected_proposals_this_quest": self.proposal_no - 1,
            "score": self.score,
            "quests": [{"quest": r.quest, "team": r.team, "fails": r.fails,
                        "fails_required": r.fails_required, "result": "success" if r.success else "fail"}
                       for r in self.results],
            "vote_history": [{"quest": p.quest, "proposal": p.number, "leader": p.leader, "team": p.team,
                              "votes": p.votes, "approved": p.approved}
                             for p in self.proposals if p.approved is not None],
            "quest_team_sizes": self.sizes,
            "winner": self.winner,
            "win_reason": self.win_reason,
        }
