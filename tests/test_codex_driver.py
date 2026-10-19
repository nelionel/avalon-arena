"""Transport/validation regressions with fake RPC responses, never fake game results."""

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from avalon.drivers import codex_local as backend
from avalon.seat import SeatConfig


class FakeRPC:
    profile = "avalon_seat"

    def __init__(self, driver, spec, outcomes):
        self.driver, self.spec, self.outcomes = driver, spec, iter(outcomes)
        self.starts, self.interrupts = 0, 0

    async def call(self, method, params):
        if method == "turn/interrupt":
            self.interrupts += 1
            return {}
        assert method == "turn/start"
        assert params["model"] == "gpt-6-luna" and params["effort"] == "high"
        self.starts += 1
        result = next(self.outcomes)
        if isinstance(result, Exception): raise result
        ident = f"api-{self.starts}"
        if result == "hang": return {"turn": {"id": ident}}
        if result in ("valid", "malformed", "short_notes"):
            Path(self.spec["notes_path"]).write_text("Too short" if result == "short_notes" else " ".join(["Explicit mock reasoning for this protocol test records the evidence and explains why this proposed move follows the current public information."] * 2))
            Path(self.spec["memory_path"]).write_text("Updated test memory")
            Path(self.spec["submit_path"]).write_text("{malformed" if result == "malformed" else '{"turn":1,"ack":true}')
            status = {"id": ident, "status": "completed"}
        else:
            status = {"id": ident, "status": "failed", "error": result}
        self.driver._notify({"method": "turn/completed", "params": {"turn": status}})
        return {"turn": {"id": ident}}


async def no_wait(_): pass


class DriverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="avalon-driver-test-")
        seat = backend.CodexSeat(Path(self.tmp.name), "P1", 0, SeatConfig(model="gpt-6-luna", effort="high"))
        seat.provision("Test instructions", "Test role", {})
        self.driver = backend.CodexDriver(seat, lambda *args, **kwargs: None)
        self.driver.thread_id = "test-thread"
        self.spec = {"turn": 1, "kind": "onboard", "you": "P1", "players": ["P1", "P2"],
                     "opened_at": time.time(), "notes_path": str(seat.work / "notes/turn_0001.md"),
                     "submit_path": str(seat.work / "outbox/turn_0001.json"), "memory_path": str(seat.work / "memory.md")}
        backend._MODEL_SLOTS = asyncio.Semaphore(2)
        backend._COOLDOWN_UNTIL = 0

    def tearDown(self): self.tmp.cleanup()

    async def run_outcomes(self, outcomes):
        self.driver.rpc = FakeRPC(self.driver, self.spec, outcomes)
        with patch.object(backend.asyncio, "sleep", no_wait):
            return await self.driver._play(self.spec, "Perform this explicit test turn")

    async def test_null_command_output_is_legal(self):
        self.driver._notify({"method": "item/completed", "params": {"item":
            {"type": "commandExecution", "id": "empty", "aggregatedOutput": None, "exitCode": 0}}})
        line = json.loads(self.driver.seat.transcript.read_text().splitlines()[-1])
        self.assertEqual(line["message"]["content"][0]["content"], "")
        self.assertFalse(line["message"]["content"][0]["is_error"])

    async def test_both_rpc_and_completed_turn_rate_limits_back_off(self):
        result = await self.run_outcomes([RuntimeError("429 Too many requests"), {"code": "rateLimitExceeded"}, "valid"])
        self.assertEqual(result["status"], "done")
        self.assertEqual(self.driver.rpc.starts, 3)
        self.assertEqual([x["seconds"] for x in self.driver.tool_events if x["event"] == "rate_limit_backoff"], [30, 60])

    async def test_rate_retries_are_bounded(self):
        result = await self.run_outcomes([{"message": "429"}] * 4)
        self.assertEqual(result["status"], "model_failed")
        self.assertEqual(self.driver.rpc.starts, 4)

    async def test_malformed_submission_is_repaired(self):
        result = await self.run_outcomes(["malformed", "valid"])
        self.assertEqual(result["status"], "done")
        self.assertEqual(self.driver.rpc.starts, 2)

    async def test_valid_action_without_valid_notes_is_not_accepted(self):
        result = await self.run_outcomes(["short_notes"] * 3)
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(self.driver.rpc.starts, 3)

    async def test_timeout_interrupts_only_the_current_api_turn(self):
        self.driver.rpc = FakeRPC(self.driver, self.spec, ["hang"])
        await self.driver.open_turn(self.spec, "Perform this explicit test turn")
        result = await self.driver.wait_done(self.spec, time.time() + .15, lambda _: "nudge")
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(self.driver.rpc.interrupts, 1)
        self.assertTrue(self.driver.task.done())


if __name__ == "__main__": unittest.main(verbosity=2)
