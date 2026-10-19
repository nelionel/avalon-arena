"""Example Avalon bot: a readable starting point. Copy it to bots/<your-name>.py and make it smarter.

Play it against the built-in scripted bots:

    bin/avalon run --players 7 --seat P1=bots/example_bot.py --seed 1

The strategy fits in a paragraph. Keep a suspicion score per player, built
from quest results and from what our role saw at night. Put trusted players
on teams and reject teams with suspicious players. As Evil, fail quests
without doubling up fail cards. As Assassin, pick the Good player whose votes
best avoided Evil teams, weighed with what the Evil huddle suggested.
"""

from collections import Counter

from avalon.bot import Bot


class ExampleBot(Bot):
    name = "example"

    def setup(self):
        # Night-phase knowledge. Merlin sees Evil (except Mordred); Evil players see each other (except Oberon).
        self.known_evil = set(self.knowledge.get("sees_evil", [])) | set(self.knowledge.get("evil_teammates", []))
        # Percival sees Merlin and Morgana without knowing which is which: lean towards trusting both.
        self.bias = {p: -0.5 for p in self.knowledge.get("merlin_candidates", [])}
        self.suspicion = {p: 0.0 for p in self.players if p != self.player}

    @property
    def evil(self) -> bool:
        return self.side == "evil"

    # -- reading the public record -------------------------------------------------
    def update(self, state):
        """Recompute suspicion from every finished quest (state["quests"])."""
        sus = {p: self.bias.get(p, 0.0) for p in self.suspicion}
        for q in state["quests"]:
            for p in q["team"]:
                if p in sus:
                    sus[p] += 2.0 * q["fails"] / len(q["team"]) if q["fails"] else -0.3
        for p in self.known_evil:
            sus[p] = 10.0
        self.suspicion = sus

    def trusted(self, exclude=()):
        """Other players, least suspicious first (ties broken by the seeded rng)."""
        return sorted((p for p in self.suspicion if p not in exclude),
                      key=lambda p: (self.suspicion[p], self.rng.random()))

    # -- one method per turn kind ------------------------------------------------------
    def propose(self, turn, state):
        self.update(state)
        size = turn["team_size"]
        if self.evil:
            # Me plus clean-looking Good players: one Evil card is usually enough, and the team looks innocent.
            team = [self.player] + self.trusted(exclude=self.known_evil)[:size - 1]
            reason = "Me plus the cleanest-looking players, so I can fail it quietly."
        else:
            team = [self.player] + self.trusted()[:size - 1]
            reason = "Me plus the least suspicious players."
        return {"team": team, "message": f"I propose {', '.join(team)}.", "reason": reason}

    def discuss(self, turn, state):
        # Discussion repeats in rounds until everyone is ready. The leader (turn["can_amend"]) may also
        # return a new "team" here to amend the proposal.
        self.update(state)
        worst = max(turn["team"], key=lambda p: self.suspicion.get(p, -1.0))
        message = None
        if worst != self.player and self.suspicion[worst] > 0.5 and not (self.evil and worst in self.known_evil):
            # A smarter Merlin would be subtler: the Assassin reads these messages too.
            message = f"I am not comfortable with {worst} on this team."
        return {"message": message, "ready_to_vote": True,
                "reason": f"Most suspicious on the team: {worst} ({self.suspicion.get(worst, 0):.1f})."}

    def vote(self, turn, state):
        self.update(state)
        team = turn["team"]
        if self.evil:
            ok = any(p == self.player or p in self.known_evil for p in team)
            reason = "Approve teams with an Evil player on them."
        elif turn["rejections_so_far"] >= 4:
            ok, reason = True, "A fifth rejection in a row hands Evil the game."
        else:
            ok = all(self.suspicion[p] < 1.0 for p in team if p != self.player)
            reason = "Approve only when nobody on the team looks suspicious."
        return {"vote": "approve" if ok else "reject", "reason": reason}

    def quest(self, turn, state):
        if "fail" not in turn["options"]:   # Good players may only play success
            return {"card": "success", "reason": "Good players always succeed."}
        # Only as many Evil players as needed fail: extra fail cards reveal how many of us are on the team.
        evil_here = sorted((p for p in turn["team"] if p == self.player or p in self.known_evil),
                           key=lambda p: int(p[1:]))
        fail = self.player in evil_here[:turn["fails_required"]] and len(evil_here) >= turn["fails_required"]
        return {"card": "fail" if fail else "success",
                "reason": f"{len(evil_here)} Evil on the team, {turn['fails_required']} fail(s) needed."}

    def merlin_guess(self, turn, state):
        """The Good player whose votes best matched the truth: rejecting Evil teams, approving clean ones."""
        evil = set(turn["evil_team"])
        score = {p: 0 for p in turn["candidates"]}
        for v in state["vote_history"]:
            tainted = bool(evil & set(v["team"]))
            for p in score:
                score[p] += (v["votes"][p] == "reject") == tainted
        return max(score, key=lambda p: (score[p], self.rng.random()))

    def evil_huddle(self, turn, state):
        guess = self.merlin_guess(turn, state)
        return {"message": f"{guess} voted as if they knew who we are.", "suggested_target": guess,
                "reason": "Merlin's votes tend to avoid Evil teams."}

    def assassinate(self, turn, state):
        votes = Counter(x["suggested_target"] for x in turn["huddle"] if x["suggested_target"])
        votes[self.merlin_guess(turn, state)] += 1      # my own read counts once too
        target = votes.most_common(1)[0][0]
        return {"target": target, "message": f"I name {target} as Merlin.",
                "reason": f"Huddle suggestions plus my own read: {dict(votes)}."}

    def reflect(self, turn, state):
        won = (turn["winner"] == self.side)
        return {"message": f"Good game. I was {self.role}; we {'won' if won else 'lost'}."}

    # -- private state the replay shows ------------------------------------------------
    def beliefs(self, turn, state):
        prior = super().beliefs(turn, state)    # night knowledge only
        return {p: 1.0 if p in self.known_evil else round(min(0.95, max(0.05, prior[p] + 0.15 * self.suspicion[p])), 2)
                for p in prior}

    def memory(self):
        rows = "\n".join(f"- {p}: {s:+.1f}" for p, s in sorted(self.suspicion.items(), key=lambda x: -x[1]))
        return f"# Memory\n\nI am {self.player}, {self.role} ({self.side}).\n\nSuspicion (higher = more likely Evil):\n{rows}\n"
