"""A plausible Avalon strategy for the mock model, so dry runs look like real games.

Everything here works only from what the seat itself could know: its role
card (parsed from ROLE.md), the public state.json (quests, vote history) and
the current turn spec. No referee data is used.
"""

from __future__ import annotations

import math
import re

EVIL_COUNT = {5: 2, 6: 2, 7: 3, 8: 3, 9: 3, 10: 4}


def parse_role(md: str) -> dict:
    role = re.search(r"# Your secret role: (.+)", md)
    evil = "Your side: **Evil" in md
    seen = re.findall(r"(?:Evil teammates|players are Evil): \*\*([^*]+)\*\*", md)
    known = [x.strip() for x in seen[0].split(",")] if seen and seen[0] != "(none)" else []
    cands = re.findall(r"One of \*\*(P\d+)\*\* and \*\*(P\d+)\*\* is Merlin", md)
    return {"role": role.group(1).strip() if role else "?", "evil": evil, "known_evil": known,
            "candidates": list(cands[0]) if cands else [], "mordred_hidden": "Mordred is in play" in md,
            "oberon_hidden": "Oberon is also Evil" in md}


def _approved_team_of(state, quest_no):
    for v in state.get("vote_history", []):
        if v["quest"] == quest_no and v["approved"]:
            return v
    return None


def suspicion(state: dict, me: str, players: list[str], i_am_good: bool) -> tuple[dict, dict]:
    """Public-evidence suspicion score per player, plus a one-line reason for each."""
    score = {p: 0.0 for p in players if p != me}
    why: dict[str, list[str]] = {p: [] for p in score}
    for q in state.get("quests", []):
        team = [p for p in q["team"] if p != me] if i_am_good else q["team"]
        if q["fails"] and team:
            for p in team:
                if p in score:
                    score[p] += 1.5 * q["fails"] / len(team)
                    why[p].append(f"was on quest {q['quest']}, which failed")
            v = _approved_team_of(state, q["quest"])
            for p, vote in (v or {}).get("votes", {}).items():
                if p in score and vote == "approve" and p not in q["team"]:
                    score[p] += 0.2
                    why[p].append(f"approved the team that failed quest {q['quest']}")
        elif not q["fails"]:
            for p in q["team"]:
                if p in score:
                    score[p] -= 0.35
    reasons = {p: (why[p][0] if why[p] else "has no bad record yet") for p in score}
    return score, reasons


def beliefs(know: dict, state: dict, me: str, players: list[str]) -> tuple[dict, dict]:
    n = len(players)
    k = EVIL_COUNT.get(n, max(1, n // 3))
    score, reasons = suspicion(state, me, players, not know["evil"])
    out: dict[str, float] = {}
    fixed: dict[str, float] = {}
    if know["evil"] and know["role"] != "Oberon":
        for p in know["known_evil"]:
            fixed[p] = 0.97
        hidden = 1 if know["oberon_hidden"] else 0
    elif know["role"] == "Merlin":
        for p in know["known_evil"]:
            fixed[p] = 0.97
        hidden = 1 if know["mordred_hidden"] else 0
    else:
        hidden = k - (0 if not know["evil"] else 1)       # Oberon knows only himself
    if know["role"] == "Percival" and len(know["candidates"]) == 2:
        for p in know["candidates"]:
            fixed[p] = 0.5
        hidden = k - 1
    unknown = [p for p in score if p not in fixed]
    if unknown:
        w = {p: math.exp(1.2 * score[p]) for p in unknown}
        tot = sum(w.values())
        for p in unknown:
            out[p] = hidden * w[p] / tot if hidden else 0.04
    out.update(fixed)
    return {p: round(min(0.97, max(0.03, v)), 2) for p, v in out.items()}, reasons


def merlin_likeness(state: dict, evil: set[str], candidates: list[str]) -> dict:
    """For Evil: who voted as if they could see us?"""
    sc = {p: 0.0 for p in candidates}
    for v in state.get("vote_history", []):
        dirty = any(p in evil for p in v["team"])
        for p, vote in v["votes"].items():
            if p not in sc:
                continue
            if dirty and vote == "reject":
                sc[p] += 1.0
            elif not dirty and vote == "approve":
                sc[p] += 0.4
            elif dirty and vote == "approve":
                sc[p] -= 0.8
    return sc


class Player:
    def __init__(self, rand, choice):
        self.rand, self.choice = rand, choice

    def act(self, spec: dict, know: dict, state: dict | None) -> tuple[dict, str, str]:
        """Return (submission core without turn, public-free private note, thinking text)."""
        state = state or {}
        me, kind, players = spec["you"], spec["kind"], spec["players"]
        bel, why = beliefs(know, state, me, players)
        evil_mates = set(know["known_evil"]) | ({me} if know["evil"] else set())
        ranked = sorted(bel, key=lambda p: bel[p])
        team = spec.get("team") or []
        others_on_team = [p for p in team if p != me]
        worst = max(others_on_team, key=lambda p: bel.get(p, 0), default=None)
        sub: dict = {}
        thought = ""
        if kind == "propose":
            size = spec["team_size"]
            pool = [p for p in ranked if p not in evil_mates] if know["evil"] else ranked
            pick = [me] + pool[:size - 1]
            sub = {"team": pick, "message": self._pitch(pick, know, bel)}
            thought = f"I lead. I take myself plus the players I trust most: {', '.join(pick)}."
        elif kind == "discuss":
            r = spec.get("round", 1)
            msg, ready = self._talk(me, team, worst, bel, why, know, evil_mates, r)
            sub = {"message": msg, "ready_to_vote": ready}
            if spec.get("can_amend") and not know["evil"] and worst and bel.get(worst, 0) > 0.6 and r >= 2:
                outsider = next((p for p in ranked if p not in team), None)
                if outsider:
                    sub["team"] = sorted([p for p in team if p != worst] + [outsider], key=lambda x: int(x[1:]))
                    sub["message"] = f"Fair point about {worst}. I'm swapping {worst} out for {outsider}."
            thought = f"Round {r}. Most suspicious on the team: {worst or 'nobody'} ({bel.get(worst, 0):.0%})."
        elif kind == "vote":
            last = spec.get("rejections_so_far", 0) >= 4
            if know["evil"] and know["role"] != "Oberon":
                ok = bool(evil_mates & set(team)) or last
            elif know["role"] == "Oberon":
                ok = me in team or last
            else:
                ok = last or worst is None or bel.get(worst, 0) < 0.5
            sub = {"vote": "approve" if ok else "reject"}
            thought = (f"Team {', '.join(team)}. Worst member {worst} at {bel.get(worst, 0):.0%}. "
                       f"{'Approve' if ok else 'Reject'}{' (fifth proposal: a reject loses the game)' if last else ''}.")
        elif kind == "quest":
            card = "success"
            if "fail" in spec["options"]:
                mates_here = sorted([p for p in team if p in evil_mates], key=lambda x: int(x[1:]))
                if spec.get("fails_required", 1) >= 2:
                    card = "fail"
                elif mates_here and mates_here[0] == me:
                    card = "fail" if (spec.get("quest", 1) > 1 or self.rand() < 0.45) else "success"
                elif know["role"] == "Oberon":
                    card = "fail" if self.rand() < 0.8 else "success"
            sub = {"card": card}
            thought = f"On quest {spec.get('quest')}. Playing {card}."
        elif kind in ("evil_huddle", "assassinate"):
            evil = set(spec.get("evil_team", [])) | evil_mates
            ml = merlin_likeness(state, evil, spec["candidates"])
            target = max(ml, key=lambda p: (ml[p], self.rand())) if ml else self.choice(spec["candidates"])
            line = f"{target} rejected the teams we were on and trusted the clean ones. That is Merlin's pattern."
            if kind == "evil_huddle":
                sub = {"message": line, "suggested_target": target}
            else:
                sub = {"target": target, "message": f"I name {target}. {line}"}
            thought = f"Merlin-likeness from votes: {ml}. Target {target}."
        elif kind == "reflect":
            top = ranked[-1] if ranked else None
            sub = {"message": f"Good game. I was {know['role']}. My strongest read was {top}."}
            thought = "Game over."
        note = self._note(spec, know, bel, why, sub)
        return sub, note, thought, bel

    def _pitch(self, pick, know, bel):
        others = [p for p in pick[1:]]
        if not others:
            return "Small team, let's see."
        return f"I'm taking {', '.join(others)}: nothing against them so far. Tell me if you see something I don't."

    def _talk(self, me, team, worst, bel, why, know, evil_mates, r):
        if know["evil"] and know["role"] != "Oberon":
            if evil_mates & set(team):
                return (self.choice([f"This team is fine. {', '.join(team)} have done nothing wrong.",
                                     "Let's not waste rounds. Approve and see the cards.",
                                     None]), True)
            target = max((p for p in bel if p not in evil_mates), key=lambda p: (why[p] != "has no bad record yet", self.rand()), default=None)
            return (f"I'm not comfortable with this team. {target} {why.get(target, 'worries me')}." if target else None, r >= 2)
        if know["role"] == "Merlin":
            bad = [p for p in team if p in know["known_evil"]]
            if bad:
                p = bad[0]
                return (self.choice([f"Something about {p} on this team bothers me. {p} has argued for every team they sit on and "
                                     "hasn't given a reason the rest of us can check. I'd rather see a different line-up.",
                                     f"I'm not convinced by {p}. Look at how {p} voted compared to what {p} said. I'll reject this one."]), r >= 2)
            return (self.choice(["I'm comfortable with this team.", None]), True)
        if worst and bel.get(worst, 0) > 0.55 and why.get(worst) == "has no bad record yet":
            return (f"No hard evidence against anyone on this team yet, so I'm reasoning from base rates: with this many "
                    f"Evil players around, a team of {len(team)} has a real chance of carrying one. {worst} is the pick I'd "
                    f"question, only because the leader hasn't explained it. Tell us why {worst}, and I'll approve.", r >= 2)
        if worst and bel.get(worst, 0) > 0.55:
            clean = [p for p in sorted(bel, key=lambda q: bel[q]) if p not in team][:1]
            swap = f" I'd swap {worst} for {clean[0]}, who {why.get(clean[0], 'has a clean record')}." if clean else ""
            return (f"I don't trust {worst} on this team: {worst} {why[worst]}. Put that next to the vote record "
                    f"and I'd put {worst} at about {bel[worst]:.0%} to be Evil.{swap} If the leader keeps {worst}, "
                    f"I'll reject and I'd like to hear {worst} explain the earlier vote.", r >= 2)
        return (self.choice(["This team looks reasonable to me. Nobody on it has been on a failed quest, and their votes "
                             "have been consistent with what they said. I'm ready to approve.",
                             None, "No objections from me. The record on these players is clean so far."]), True)

    def _note(self, spec, know, bel, why, sub):
        top = sorted(bel, key=lambda p: -bel[p])[:3]
        reads = "; ".join(f"{p} {bel[p]:.0%} ({why.get(p, 'known from my role card')})" for p in top)
        act = {k: v for k, v in sub.items() if k not in ("message",)}
        return (f"# Turn {spec['turn']} ({spec['kind']}) - private reasoning\n\n"
                f"I am {know['role']}{' (Evil)' if know['evil'] else ' (Good)'}.\n\n"
                f"Most suspicious right now: {reads}.\n\n"
                f"Action: {act or 'message only'}. "
                + ("I want quests to fail without exposing my team." if know["evil"] else
                   "I want to keep the players I suspect off the quest.") + "\n")


def memory_md(spec: dict, know: dict, state: dict | None, bel: dict, why: dict) -> str:
    state = state or {}
    lines = [f"# Memory - {spec['you']}", "", "## Certain facts", f"- I am {know['role']} ({'Evil' if know['evil'] else 'Good'})."]
    if know["known_evil"]:
        lines.append(f"- Known Evil: {', '.join(know['known_evil'])}.")
    lines += ["", "## Record"]
    for q in state.get("quests", []):
        lines.append(f"- Quest {q['quest']}: team {', '.join(q['team'])}, {q['fails']} fail(s) -> {q['result']}.")
    for v in state.get("vote_history", [])[-6:]:
        app = [p for p, x in v["votes"].items() if x == "approve"]
        lines.append(f"- Q{v['quest']} P{v['proposal']} leader {v['leader']} team {', '.join(v['team'])}: "
                     f"{'approved' if v['approved'] else 'rejected'} (approve: {', '.join(app)}).")
    lines += ["", "## Beliefs (P Evil)"] + [f"- {p}: {bel[p]:.0%} - {why.get(p, 'role card')}" for p in sorted(bel, key=lambda x: -bel[x])]
    lines += ["", "## Plan", "- Keep the most suspicious players off quests; update after every result." if not know["evil"]
              else "- Get Evil onto quests, fail when it counts, deflect suspicion onto Good players."]
    return "\n".join(lines) + "\n"
