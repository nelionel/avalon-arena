"""In-process heuristic bots (no Claude): fast engine tests and a stats baseline.

They obey the same protocol as the Claude players: read their inbox spec,
write a private decision document and an outbox submission, and only know
what their own role card tells them plus the public record.
"""

from __future__ import annotations

import json
import os
import random
import time

from ..roles import EVIL, ROLES


class ScriptedDriver:
    kind = "scripted"

    def __init__(self, seat, assignment_view: dict, log, rng: random.Random):
        self.seat, self.log, self.rng = seat, log, rng
        self.view = assignment_view           # night-phase knowledge only (roles.knowledge output)
        self.suspicion: dict[str, float] = {}
        self.last = None

    @property
    def player(self) -> str:
        return self.seat.player

    async def start(self):
        self.log("seat_started", player=self.player, pid=os.getpid(), session_id=None)

    async def open_turn(self, spec, prompt):
        state = json.loads((self.seat.public / "state.json").read_text())
        sub, why = self.decide(spec, state)
        notes = (
            f"# Turn {spec['turn']} ({spec['kind']})\n\n"
            f"Decision rationale: {why}\n\n"
            f"The scripted baseline used its own role card and the public record of "
            f"{len(state.get('quests', []))} completed quests. It did not read other players' "
            f"private files. Its suspicion scores are heuristic weights, not calibrated "
            f"Bayesian probabilities.\n\nSelected action: {json.dumps(sub, sort_keys=True)}\n\n"
            f"Current suspicion scores: {json.dumps(self.suspicion, sort_keys=True)}\n"
        )
        files = [(spec["notes_path"], notes), (spec["submit_path"], json.dumps(sub, indent=2))]
        if spec.get("memory_path"):
            files.append((spec["memory_path"], f"# Memory\n\nTurn {spec['turn']}: {why}\nSuspicion: {json.dumps(self.suspicion)}\n"))
        for path, text in files:
            with open(path, "w") as f:
                f.write(text)
            if self.seat.uid != os.getuid():
                os.chown(path, self.seat.uid, self.seat.uid)
        self.last = {"ts": time.time(), "event": "turn_done", "turn": spec["turn"], "valid": True, "submission": sub}

    async def wait_done(self, spec, deadline, nudge_text):
        return {"status": "done", "record": self.last}

    async def screen(self, ansi=True, history=80):
        return None

    async def pid(self):
        return None

    async def stop(self):
        pass

    # -- policy --------------------------------------------------------------
    def _update(self, state):
        sus = {p: 0.0 for p in state.get("leader_order", [])}
        for q in state["quests"]:
            if q["fails"]:
                for p in q["team"]:
                    sus[p] = sus.get(p, 0) + q["fails"] / len(q["team"]) * 2
            else:
                for p in q["team"]:
                    sus[p] = sus.get(p, 0) - 0.3
        for p in self.view.get("sees_evil", []) + self.view.get("evil_teammates", []):
            sus[p] = sus.get(p, 0) + 100
        sus.pop(self.player, None)
        self.suspicion = {p: round(v, 2) for p, v in sus.items()}

    def decide(self, spec, state):
        sub, why = self._decide(spec, state)
        if spec["kind"] in ("propose", "discuss", "vote", "quest", "evil_huddle", "assassinate"):
            import math
            sub["beliefs"] = {p: round(1 / (1 + math.exp(-(v - 0.5) * 2.2)), 2) for p, v in self.suspicion.items()}
            for p in spec["players"]:
                if p != self.player and p not in sub["beliefs"]:
                    sub["beliefs"][p] = 0.4
        return sub, why

    def _decide(self, spec, state):
        self._update(state)
        me, k, evil = self.player, spec["kind"], ROLES[self.view["role"]].side == EVIL
        mates = set(self.view.get("evil_teammates", []))
        trusted = sorted(self.suspicion, key=lambda p: self.suspicion[p] + self.rng.random() * 0.5)
        t = {"turn": spec["turn"]}
        if k == "onboard":
            return {**t, "ack": True}, f"I am {self.view['role']}. Heuristic bot."
        if k == "propose":
            if evil:
                pool = [p for p in trusted if p not in mates]
                team = [me] + pool[:spec["team_size"] - 1]
            else:
                team = [me] + [p for p in trusted][:spec["team_size"] - 1]
            return {**t, "team": team, "message": f"Proposing {', '.join(team)}."}, "Least suspicious players."
        if k == "discuss":
            team = spec.get("team", [])
            worst = max(team, key=lambda p: self.suspicion.get(p, 0), default=None)
            msg = None
            if worst and worst != me and self.suspicion.get(worst, 0) > 0.5 and not (evil and worst in mates):
                msg = f"I am worried about {worst} on this team."
            return {**t, "message": msg, "ready_to_vote": True}, f"Most suspicious on team: {worst}."
        if k == "vote":
            team = spec["team"]
            if spec.get("rejections_so_far", 0) >= 4 and not evil:
                return {**t, "vote": "approve"}, "Fifth proposal: approving to avoid an automatic loss."
            if evil:
                ok = any(p in mates or p == me for p in team)
            else:
                ok = all(self.suspicion.get(p, 0) < 1.0 for p in team if p != me)
            return {**t, "vote": "approve" if ok else "reject"}, "Vote by team suspicion."
        if k == "quest":
            fail = "fail" in spec["options"] and (spec["quest"] > 1 or self.rng.random() < 0.5)
            return {**t, "card": "fail" if fail else "success"}, "Fail when it is not too obvious."
        if k == "evil_huddle":
            target = self.rng.choice(spec["candidates"])
            return {**t, "message": f"I think {target} is Merlin.", "suggested_target": target}, "Guess."
        if k == "assassinate":
            target = self.rng.choice(spec["candidates"])
            return {**t, "target": target, "message": None}, "Random among good players."
        return {**t, "message": None}, "Game over."
