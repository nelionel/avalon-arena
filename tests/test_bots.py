"""Local Python bots: no-root seats, per-seat assignment, the bot view, and clear failures.

Run: python3 tests/test_bots.py
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from avalon import cli, referee  # noqa: E402
from avalon.audit import audit_game  # noqa: E402
from avalon.drivers.local_bot import HIDDEN_SPEC_KEYS, BotError  # noqa: E402
from avalon.roles import knowledge  # noqa: E402

EXAMPLE = str(ROOT / "bots" / "example_bot.py")

SPY = '''
import json
from pathlib import Path
from avalon.bot import Bot

LOG = Path(__file__).with_suffix(".jsonl")

class SpyBot(Bot):
    """Records what it is shown; plays so that Good wins and the Evil huddle happens."""
    def act(self, turn, state):
        with LOG.open("a") as f:
            f.write(json.dumps({"player": self.player, "knowledge": self.knowledge, "turn": turn,
                                "events": len(state["events"])}) + "\\n")
        if turn["kind"] == "vote":
            return {"vote": "approve"}
        if turn["kind"] == "quest":
            return {"card": "success"}
        if turn["kind"] == "evil_huddle":
            return {"message": "pick the first", "suggested_target": turn["candidates"][0]}
        return super().act(turn, state)
'''


class BotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="avalon-bot-test-")
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(referee, "GAMES", self.root / "games"),
                        patch.object(referee, "SLOTS", self.root / "slots")]
        for p in self.patches:
            p.start()

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 10   # game file I/O is not a slow-callback bug

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def bot_file(self, name, source):
        path = self.root / name
        path.write_text(textwrap.dedent(source))
        return str(path)

    async def play(self, **kw):
        kw = {"players": 5, "driver": "bots", "voice": False, "seed": 1, **kw}
        r = referee.Referee(referee.GameConfig(**kw))
        await r.run()
        return r

    def summary(self, r):
        return json.loads((r.ref / "summary.json").read_text())

    async def test_example_bot_plays_every_table_size(self):
        for n in range(5, 11):
            r = await self.play(players=n, seats={"P1": EXAMPLE, "P2": EXAMPLE}, fill="random", seed=n)
            s = self.summary(r)
            self.assertIn(s["winner"], ("good", "evil"))
            self.assertEqual(s["bots"]["P1"], "example")
            self.assertEqual(s["bots"]["P3"], "random")
            report = audit_game(r.dir)
            self.assertEqual(report["summary"]["failed"], [], n)

    async def test_scripted_driver_runs_on_local_seats(self):
        r = await self.play(driver="scripted", players=7)
        seats = json.loads((r.ref / "seats.json").read_text())
        self.assertEqual({s["uid"] for s in seats.values()}, {os.getuid()})
        self.assertEqual(set(self.summary(r)["bots"].values()), {"scripted"})

    async def test_same_seed_replays_the_same_game(self):
        def events(r):
            return [{k: v for k, v in json.loads(line).items() if k != "ts"}
                    for line in (r.dir / "public" / "events.jsonl").read_text().splitlines()]
        a = await self.play(players=7, seats={"P1": EXAMPLE}, seed=11, game_id="a")
        b = await self.play(players=7, seats={"P1": EXAMPLE}, seed=11, game_id="b")
        self.assertEqual(events(a), events(b))

    async def test_bot_sees_only_its_own_view(self):
        spy = self.bot_file("spy_bot.py", SPY)
        r = await self.play(players=7, fill=spy, seed=3)
        self.assertEqual(self.summary(r)["winner"], "evil")       # Good won 3 quests; the Assassin then struck
        roles = json.loads((r.ref / "roles.json").read_text())
        records = [json.loads(line) for line in Path(spy).with_suffix(".jsonl").read_text().splitlines()]
        kinds = {x["turn"]["kind"] for x in records}
        self.assertTrue({"onboard", "propose", "discuss", "vote", "quest", "evil_huddle", "assassinate", "reflect"} <= kinds)
        for x in records:
            p, turn = x["player"], x["turn"]
            self.assertEqual(x["knowledge"], knowledge(p, roles))
            self.assertEqual(turn["you"], p)
            self.assertFalse(HIDDEN_SPEC_KEYS & turn.keys(), turn.keys())
            self.assertEqual("huddle" in turn, turn["kind"] in ("evil_huddle", "assassinate"))
        shot = [x["turn"] for x in records if x["turn"]["kind"] == "assassinate"][0]
        self.assertTrue(shot["huddle"] and all(h["suggested_target"] == shot["candidates"][0] for h in shot["huddle"]))
        self.assertGreater(max(x["events"] for x in records), 10)

    async def test_invalid_move_stops_the_game_before_publication(self):
        bad = self.bot_file("bad_bot.py", """
            from avalon.bot import Bot
            class TooBig(Bot):
                def propose(self, turn, state):
                    return {"team": self.players[:turn["team_size"] + 1]}
        """)
        with self.assertRaises(BotError) as e:
            await self.play(seats={"P1": bad}, first_leader="P1", game_id="bad")
        self.assertIn("P1 (bad_bot:TooBig) made an invalid propose move", str(e.exception))
        self.assertIn("must have exactly 2 players", str(e.exception))
        events = (self.root / "games" / "bad" / "public" / "events.jsonl").read_text()
        self.assertNotIn('"proposal"', events)
        status = json.loads((self.root / "games" / "bad" / "referee" / "status.json").read_text())
        self.assertEqual(status["status"], "error")

    async def test_bot_exception_names_the_seat_and_keeps_the_traceback(self):
        crash = self.bot_file("crash_bot.py", """
            from avalon.bot import Bot
            class Crash(Bot):
                def vote(self, turn, state):
                    return 1 / 0
        """)
        with self.assertRaises(BotError) as e:
            await self.play(seats={"P2": crash}, game_id="crash")
        self.assertIn("P2 (crash_bot:Crash) raised ZeroDivisionError", str(e.exception))
        self.assertIn("crash_bot.py", str(e.exception))
        self.assertTrue((self.root / "games" / "crash" / "players" / "p2" / "audit" / "bot-errors.log").exists())

    async def test_bad_bot_specs_fail_before_any_game_files(self):
        two = self.bot_file("two_bots.py", """
            from avalon.bot import Bot
            class A(Bot): pass
            class B(Bot): pass
        """)
        cases = {str(self.root / "missing.py"): "existing .py file",
                 two: "defines 2 Bot subclasses",
                 two + ":Nope": "Nope is not a subclass"}
        for spec, msg in cases.items():
            with self.assertRaises(BotError) as e:
                await self.play(seats={"P1": spec})
            self.assertIn(msg, str(e.exception))
        with self.assertRaises(ValueError) as e:
            await self.play(seats={"P9": "random"})
        self.assertIn("P9", str(e.exception))
        self.assertFalse((self.root / "games").exists() and any((self.root / "games").iterdir()))
        r = await self.play(seats={"P1": two + ":B"})
        self.assertEqual(self.summary(r)["bots"]["P1"], "two_bots:B")


class CliTests(unittest.TestCase):
    def args(self, **kw):
        return argparse.Namespace(**{"seat": [], "fill": None, "driver": None, **kw})

    def test_seat_flags_choose_the_bots_driver(self):
        self.assertEqual(cli.driver_of(self.args()), "claude")
        self.assertEqual(cli.driver_of(self.args(seat=["P1=random"])), "bots")
        self.assertEqual(cli.driver_of(self.args(fill="random")), "bots")
        with self.assertRaises(SystemExit):
            cli.driver_of(self.args(seat=["P1=random"], driver="codex"))

    def test_seat_paths_resolve_from_where_the_command_ran(self):
        with patch.dict(os.environ, {"AVALON_CWD": str(ROOT / "bots")}):
            seats = cli.parse_seats(["p1=example_bot.py", "3=random", "P4=example_bot.py:ExampleBot"])
        self.assertEqual(seats, {"P1": EXAMPLE, "P3": "random", "P4": EXAMPLE + ":ExampleBot"})
        for bad in (["P1"], ["P1=random", "p1=random"], ["X=random"]):
            with self.assertRaises(SystemExit):
                cli.parse_seats(bad)


if __name__ == "__main__":
    unittest.main()
