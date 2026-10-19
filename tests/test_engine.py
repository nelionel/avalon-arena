"""Rules checks for the Avalon engine. Run: python3 tests/test_engine.py"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from avalon.engine import ASSASSINATE, DISCUSS, HUDDLE, PROPOSE, QUEST, REFLECT, VOTE, Game  # noqa: E402
from avalon.roles import PRESETS, TEAM_SIZES, fails_required, knowledge, validate_roles  # noqa: E402

failures = 0


def check(name, cond):
    global failures
    print(("PASS " if cond else "FAIL ") + name)
    failures += not cond


def game(roles, leader="P1", **kw):
    a = {f"P{i + 1}": r for i, r in enumerate(roles)}
    g = Game(a, first_leader=leader, rng=random.Random(1), **kw)
    g.apply({p: {"ack": True} for p in g.players})
    return g


def propose(g, team):
    g.apply({g.leader: {"team": team, "message": None}})


def discuss_all_ready(g):
    g.apply({p: {"message": "ok", "ready_to_vote": True} for p in g.players})


def vote(g, approve):
    g.apply({p: {"vote": "approve" if (approve is True or p in approve) else "reject"} for p in g.players})


# --- setup tables
check("presets are legal for 5-10 players", all(not validate_roles(PRESETS[n]) for n in range(5, 11)))
check("team sizes match the official table", TEAM_SIZES[5] == [2, 3, 2, 3, 3] and TEAM_SIZES[10] == [3, 4, 4, 5, 5])
check("quest 4 needs two fails only with 7+ players", fails_required(6, 3) == 1 and fails_required(7, 3) == 2
      and fails_required(10, 0) == 1)

# --- night-phase knowledge
a = {f"P{i + 1}": r for i, r in enumerate(PRESETS[10])}   # merlin, percival, 4 servants, morgana, assassin, mordred, oberon
k = {p: knowledge(p, a) for p in a}
merlin, percival = "P1", "P2"
check("Merlin sees Morgana, Assassin and Oberon but not Mordred", k[merlin]["sees_evil"] == ["P7", "P8", "P10"])
check("Percival sees exactly Merlin and Morgana", k[percival]["merlin_candidates"] == ["P1", "P7"])
check("Evil (non-Oberon) see each other but not Oberon", k["P7"]["evil_teammates"] == ["P8", "P9"])
check("Oberon sees nobody", k["P10"]["evil_teammates"] == [])
check("Servants know nothing", set(k["P3"]) == {"role", "side"})

# --- votes, rotation, rejection track
g = game(PRESETS[7])
check("first phase after onboarding is a proposal", g.phase == PROPOSE)
propose(g, ["P1", "P2"])
discuss_all_ready(g)
check("all ready ends discussion after one round", g.phase == VOTE)
vote(g, {"P1", "P2", "P3"})                    # 3 of 7: not a majority
check("3/7 approvals is a rejection; leadership passes", g.phase == PROPOSE and g.leader == "P2")
for i in range(3):
    propose(g, ["P1", "P2"]); discuss_all_ready(g); vote(g, set())
check("four rejections leave the game going", g.winner is None and g.proposal_no == 5)
propose(g, ["P1", "P2"]); discuss_all_ready(g); vote(g, {"P1", "P2", "P3"})
check("the fifth rejection in one quest wins for Evil", g.winner == "evil" and g.win_reason == "five_rejections")

# --- strict majority and quests
g = game(PRESETS[6])
propose(g, ["P1", "P2"]); discuss_all_ready(g); vote(g, {"P1", "P2", "P3"})
check("3/6 approvals is not a strict majority", g.phase == PROPOSE)
propose(g, ["P1", "P2"]); discuss_all_ready(g); vote(g, {"P1", "P2", "P3", "P4"})
check("4/6 approvals sends the team on the quest", g.phase == QUEST)
try:
    g.apply({"P1": {"card": "fail"}, "P2": {"card": "success"}})
    check("Good players cannot play fail", False)
except ValueError:
    check("Good players cannot play fail", True)

# --- discussion limits
g = game(PRESETS[5], max_rounds=10)
propose(g, ["P1", "P2"])
for r in range(10):
    if g.phase != DISCUSS:
        break
    g.apply({p: {"message": f"round {r}", "ready_to_vote": False} for p in g.players})
check("discussion stops after at most 10 rounds", g.phase == VOTE and g.current.rounds and len(g.current.rounds) == 10)
g = game(PRESETS[5])
propose(g, ["P1", "P2"])
g.apply({p: {"message": None, "ready_to_vote": False} for p in g.players})
check("a silent round ends discussion", g.phase == VOTE)
g = game(PRESETS[5])
propose(g, ["P1", "P2"])
g.apply({p: {"message": "x", "ready_to_vote": True, **({"team": ["P1", "P3"]} if p == "P1" else {})} for p in g.players})
check("an amendment forces another round even if all are ready", g.phase == DISCUSS and g.current.team == ["P1", "P3"])

# --- two-fail quest, assassination
roles = ["merlin", "percival", "servant", "servant", "morgana", "assassin", "oberon"]   # P5-P7 evil
g = game(roles)
def play_quest(g, team, cards):
    propose(g, team); discuss_all_ready(g); vote(g, True)
    g.apply({p: {"card": cards.get(p, "success")} for p in team})
play_quest(g, ["P1", "P2"], {})
play_quest(g, ["P1", "P2", "P3"], {})
play_quest(g, ["P1", "P2", "P3"], {})
check("three successes lead to the Evil huddle", g.phase == HUDDLE)
g.apply({p: {"message": "it's P1", "suggested_target": "P1"} for p in g.evil_players})
check("the huddle leads to the assassination", g.phase == ASSASSINATE)
g.apply({"P6": {"target": "P1", "message": None}})
check("naming Merlin wins for Evil", g.winner == "evil" and g.win_reason == "merlin_assassinated" and g.phase == REFLECT)

g = game(roles)
play_quest(g, ["P1", "P2"], {})
play_quest(g, ["P5", "P6", "P3"], {"P5": "fail"})
play_quest(g, ["P1", "P2", "P3"], {})
play_quest(g, ["P5", "P6", "P1", "P2"], {"P5": "fail"})
check("one fail on quest 4 (7 players) is not enough", g.results[3].success is True)
check("Good wins three quests, then the Assassin may still strike", g.phase == HUDDLE)

print(f"\n{failures} failure(s)")
sys.exit(1 if failures else 0)
