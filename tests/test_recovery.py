"""Fault-injection tests of real referee/files/Git with explicitly fake model drivers."""

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from avalon import referee, publicrepo
from avalon.persistence import write_json


class FakeModelDriver:
    """Protocol fixture only; never represented as real model output."""
    failures = {}
    calls = []

    def __init__(self, seat, log):
        self.seat, self.log = seat, log
        self.boundary_checks = {}

    async def start(self):
        path = self.seat.audit / "session.json"
        write_json(path, {"thread_id": "fake-test-" + self.seat.player,
                         "model": self.seat.cfg.model, "ephemeral": False}, once=True)

    async def open_turn(self, spec, prompt):
        self.calls.append((spec["turn"], self.seat.player))
        k = spec["kind"]
        action = {"turn": spec["turn"]}
        if k == "onboard": action["ack"] = True
        elif k == "propose": action.update(team=spec["players"][:spec["team_size"]], message=None)
        elif k == "discuss": action.update(message=None, ready_to_vote=True)
        elif k == "vote": action["vote"] = "approve"
        elif k == "quest": action["card"] = "success"
        elif k == "evil_huddle": action.update(message=None, suggested_target=None)
        elif k == "assassinate": action.update(target=spec["candidates"][0], message=None)
        else: action["message"] = None
        if k not in ("onboard", "reflect"):
            action["beliefs"] = {p: .4 for p in spec["players"] if p != spec["you"]}
        Path(spec["submit_path"]).write_text(json.dumps(action))
        Path(spec["notes_path"]).write_text(" ".join(["This explicit test fixture checks durable accepted moves and publication barriers using a fake model, without making any strategic quality claims."] * 2))
        Path(spec["memory_path"]).write_text(f"Fake model memory updated for turn {spec['turn']}")
        self.last = {"ts": time.time()}

    async def wait_done(self, spec, deadline, nudge_text):
        failure = self.failures.get((spec["turn"], self.seat.player))
        if failure == "short_notes":
            Path(spec["notes_path"]).write_text("Too short.")
        return {"status": failure if failure and failure != "short_notes" else "done", "record": self.last}

    def activity(self): return {"tool_calls": 0, "reads": []}
    async def screen(self): return "Explicit fake model fixture"
    async def pid(self): return None
    async def stop(self): pass


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="avalon-recovery-test-")
        self.root = Path(self.tmp.name)
        FakeModelDriver.failures, FakeModelDriver.calls = {}, []
        self.patches = [patch.object(referee, "GAMES", self.root / "games"),
                        patch.object(referee, "SLOTS", self.root / "slots"),
                        patch.object(referee, "CodexDriver", FakeModelDriver)]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def new(self, limit=1, players=5):
        return referee.Referee(referee.GameConfig(players=players, driver="codex", effort="high",
                    game_id="fixture", seed=7, max_turns=limit, voice=False))

    def git(self, r, *args): return publicrepo._git(r.dir / "public", *args).strip()

    async def test_receipts_keep_accepted_moves_and_only_retry_missing_player(self):
        r = self.new()
        FakeModelDriver.failures[(1, "P3")] = "timeout"
        with self.assertRaisesRegex(RuntimeError, "unpublished"):
            await r.run()
        self.assertEqual(self.git(r, "rev-list", "--count", "HEAD"), "1")
        self.assertEqual((r.dir / "public/events.jsonl").read_text(), "")
        receipt_path = r.ref / "pending/0001/P1.json"
        receipt = receipt_path.read_bytes()
        # A saved accepted action is independent of a later edit to the mutable outbox.
        (r.seats["P1"].work / "outbox/turn_0001.json").write_text('{"turn":1,"ack":false}')
        FakeModelDriver.failures.clear()
        resumed = referee.Referee.from_saved(r.gid, max_turns=1)
        await resumed.run()
        self.assertEqual(receipt_path.read_bytes(), receipt)
        self.assertEqual(FakeModelDriver.calls.count((1, "P1")), 1)
        self.assertEqual(FakeModelDriver.calls.count((1, "P3")), 2)
        self.assertEqual(self.git(r, "rev-list", "--count", "HEAD"), "2")
        self.assertEqual(len(resumed.transactions), 1)
        self.assertTrue(resumed.transactions[0]["actions"]["P1"]["ack"])

    async def test_valid_json_with_invalid_notes_is_not_accepted(self):
        r = self.new()
        FakeModelDriver.failures[(1, "P2")] = "short_notes"
        with self.assertRaisesRegex(RuntimeError, "unpublished"):
            await r.run()
        self.assertFalse((r.ref / "pending/0001/P2.json").exists())
        self.assertEqual((r.dir / "public/events.jsonl").read_text(), "")

    async def test_crash_before_publication_replays_saved_transaction(self):
        r = self.new()
        with patch.object(publicrepo.PublicRepo, "publish", side_effect=RuntimeError("injected before publish")):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                await r.run()
        self.assertTrue((r.ref / "transactions/0001.json").exists())
        calls = list(FakeModelDriver.calls)
        resumed = referee.Referee.from_saved(r.gid, max_turns=1)
        await resumed.run()
        self.assertEqual(FakeModelDriver.calls, calls)
        self.assertEqual(self.git(r, "rev-list", "--count", "HEAD"), "2")

    async def test_crash_during_public_files_does_not_duplicate_events(self):
        r = self.new()
        original = publicrepo.atomic_text
        def interrupted(path, text, **kw):
            if path.name == "state.json": raise RuntimeError("injected mid-publish")
            return original(path, text, **kw)
        with patch.object(publicrepo, "atomic_text", interrupted):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                await r.run()
        await referee.Referee.from_saved(r.gid, max_turns=1).run()
        public = (r.dir / "public/events.jsonl").read_text().splitlines()
        self.assertEqual(sum(json.loads(x)["type"] == "game_started" for x in public), 1)
        self.assertEqual(self.git(r, "rev-list", "--count", "HEAD"), "2")

    async def test_crash_after_commit_preserves_commit_identity(self):
        r = self.new()
        with patch.object(r, "sync_journals", side_effect=RuntimeError("injected after commit")):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                await r.run()
        head = self.git(r, "rev-parse", "HEAD")
        resumed = referee.Referee.from_saved(r.gid, max_turns=1)
        await resumed.run()
        self.assertEqual(head, self.git(r, "rev-parse", "HEAD"))
        self.assertEqual(len((r.ref / "turns.jsonl").read_text().splitlines()), 1)

    async def test_live_lease_blocks_second_referee_without_touching_status(self):
        r = self.new()
        await r.setup()
        try:
            before = (r.ref / "status.json").read_bytes()
            with self.assertRaisesRegex(RuntimeError, "live referee"):
                await referee.Referee.from_saved(r.gid).run()
            self.assertEqual(before, (r.ref / "status.json").read_bytes())
        finally:
            await r.teardown()

    async def test_existing_game_id_cannot_overwrite_a_saved_run(self):
        r = self.new()
        await r.run()
        before = (r.ref / "roles.json").read_bytes(), self.git(r, "rev-parse", "HEAD")
        with self.assertRaises(FileExistsError): await self.new().run()
        self.assertEqual(before, ((r.ref / "roles.json").read_bytes(), self.git(r, "rev-parse", "HEAD")))

    async def test_partial_derived_journal_is_rebuilt_from_immutable_records(self):
        r = self.new(limit=2)
        await r.run()
        journal = r.ref / "turns.jsonl"
        complete = journal.read_bytes()
        journal.write_bytes(complete[:-35])
        await referee.Referee.from_saved(r.gid, max_turns=2).run()
        self.assertEqual(journal.read_bytes(), complete)


if __name__ == "__main__": unittest.main(verbosity=2)
