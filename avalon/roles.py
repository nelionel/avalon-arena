"""Avalon roles, standard setup tables, and night-phase knowledge."""

from __future__ import annotations

from dataclasses import dataclass

GOOD, EVIL = "good", "evil"


@dataclass(frozen=True)
class Role:
    key: str
    name: str
    side: str
    blurb: str


ROLES: dict[str, Role] = {r.key: r for r in [
    Role("merlin", "Merlin", GOOD,
         "You know who (most of) the Evil players are. If Good wins three quests, the Assassin gets "
         "one guess at who Merlin is - if they find you, Evil wins. Guide Good without exposing yourself."),
    Role("percival", "Percival", GOOD,
         "You see two players who might be Merlin: one is the real Merlin, the other is Morgana "
         "pretending. Work out which is which and protect Merlin."),
    Role("servant", "Loyal Servant of Arthur", GOOD,
         "You have no special knowledge. Use votes, quest results and discussion to find Evil."),
    Role("morgana", "Morgana", EVIL,
         "You appear to Percival as a possible Merlin. Use that to confuse Good."),
    Role("assassin", "Assassin", EVIL,
         "If Good completes three quests, you name one player as Merlin. A correct guess wins the game for Evil."),
    Role("mordred", "Mordred", EVIL,
         "Merlin cannot see you. You are the hardest Evil player for Good to identify."),
    Role("oberon", "Oberon", EVIL,
         "You are Evil, but you do not know the other Evil players and they do not know you. "
         "Merlin can see you. Sabotage quests and look for your teammates."),
    Role("minion", "Minion of Mordred", EVIL,
         "A loyal Evil player. You know your Evil teammates (except Oberon)."),
]}

# Quest team sizes and evil counts from the official rules.
TEAM_SIZES = {5: [2, 3, 2, 3, 3], 6: [2, 3, 4, 3, 4], 7: [2, 3, 3, 4, 4],
              8: [3, 4, 4, 5, 5], 9: [3, 4, 4, 5, 5], 10: [3, 4, 4, 5, 5]}
EVIL_COUNT = {5: 2, 6: 2, 7: 3, 8: 3, 9: 3, 10: 4}

PRESETS = {
    5: ["merlin", "percival", "servant", "morgana", "assassin"],
    6: ["merlin", "percival", "servant", "servant", "morgana", "assassin"],
    7: ["merlin", "percival", "servant", "servant", "morgana", "assassin", "oberon"],
    8: ["merlin", "percival", "servant", "servant", "servant", "morgana", "assassin", "minion"],
    9: ["merlin", "percival", "servant", "servant", "servant", "servant", "morgana", "assassin", "mordred"],
    10: ["merlin", "percival", "servant", "servant", "servant", "servant",
         "morgana", "assassin", "mordred", "oberon"],
}


def fails_required(n_players: int, quest_index: int) -> int:
    """The 4th quest (index 3) needs two fails with 7+ players."""
    return 2 if n_players >= 7 and quest_index == 3 else 1


def validate_roles(keys: list[str]) -> list[str]:
    """Return a list of problems with a role list (empty when it is legal)."""
    n = len(keys)
    probs = []
    if n not in TEAM_SIZES:
        return [f"Avalon needs 5-10 players (got {n})"]
    bad = [k for k in keys if k not in ROLES]
    if bad:
        return [f"unknown roles: {bad} (known: {sorted(ROLES)})"]
    evil = sum(ROLES[k].side == EVIL for k in keys)
    if evil != EVIL_COUNT[n]:
        probs.append(f"{n} players need {EVIL_COUNT[n]} evil roles (got {evil})")
    for unique in ("merlin", "percival", "morgana", "assassin", "mordred", "oberon"):
        if keys.count(unique) > 1:
            probs.append(f"at most one {unique}")
    if "merlin" in keys and "assassin" not in keys:
        probs.append("Merlin requires an Assassin")
    if "percival" in keys and "merlin" not in keys:
        probs.append("Percival requires Merlin")
    return probs


def roles_in_play_summary(keys: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {GOOD: [], EVIL: []}
    for k in sorted(set(keys), key=lambda k: list(ROLES).index(k)):
        c = keys.count(k)
        out[ROLES[k].side].append(ROLES[k].name + (f" x{c}" if c > 1 else ""))
    return out


def knowledge(player: str, assignment: dict[str, str]) -> dict:
    """What `player` learns during the night phase.

    Returns structured facts; prompts.py renders them into the role card.
    """
    role = assignment[player]
    evil_seen_by_evil = sorted((p for p, r in assignment.items()
                                if ROLES[r].side == EVIL and r != "oberon" and p != player), key=_seat)
    info: dict = {"role": role, "side": ROLES[role].side}
    if role == "merlin":
        info["sees_evil"] = sorted((p for p, r in assignment.items()
                                    if ROLES[r].side == EVIL and r != "mordred"), key=_seat)
        info["hidden_from_you"] = ["Mordred"] if "mordred" in assignment.values() else []
    elif role == "percival":
        cands = [p for p, r in assignment.items() if r in ("merlin", "morgana")]
        info["merlin_candidates"] = sorted(cands, key=_seat)
        info["morgana_in_play"] = "morgana" in assignment.values()
    elif ROLES[role].side == EVIL and role != "oberon":
        info["evil_teammates"] = evil_seen_by_evil
        info["oberon_in_play"] = "oberon" in assignment.values()
    elif role == "oberon":
        info["evil_teammates"] = []
    return info


def _seat(p: str) -> int:
    return int(p[1:])
