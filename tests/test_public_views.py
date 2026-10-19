"""Public exports must not reveal private huddle participants or setup metadata."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from avalon.dashboard import data


class PublicViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="avalon-public-test-")
        self.root = Path(self.tmp.name)
        self.game = self.root / "fixture"
        (self.game / "referee").mkdir(parents=True)
        (self.game / "public").mkdir()
        self.roles = {"P1": "merlin", "P2": "servant", "P3": "percival", "P4": "morgana", "P5": "assassin"}
        self.status = {"status": "running", "driver": "codex", "model": "gpt-6-luna", "turn": 4,
                       "phase": "evil_huddle", "actors": ["P4", "P5"], "seats_done": {"P4": {"status": "done"}},
                       "config": {"seed": 74123, "credentials_file": "PRIVATE_MARKER/credentials.env"}}
        self.write("referee/status.json", self.status)
        self.write("referee/roles.json", self.roles)
        self.write("public/game.json", {"quest_team_sizes": [2, 3, 2, 3, 3]})
        self.turns = [
            {"turn": 1, "kind": "propose", "actors": ["P1"], "seats": {"P1": {"status": "done", "defaulted": False,
                "reads": ["PRIVATE_MARKER"], "submission": {"team": ["P1", "P2"], "beliefs": {"P4": 1}, "role_guesses": {"P4": "morgana"}}}}},
            {"turn": 2, "kind": "quest", "actors": ["P1", "P4"], "seats": {"P1": {"submission": {"card": "success"}}, "P4": {"submission": {"card": "fail"}}}},
            {"turn": 3, "kind": "evil_huddle", "actors": ["P4", "P5"], "seats": {"P4": {"submission": {"message": "PRIVATE_MARKER"}}, "P5": {"submission": None}}},
        ]
        (self.game / "referee/turns.jsonl").write_text("".join(json.dumps(t) + "\n" for t in self.turns))
        (self.game / "public/events.jsonl").write_text("")
        self.patch = patch.object(data, "GAMES", self.root)
        self.patch.start()
        data._MEMORY_CACHE.clear()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def write(self, name, value):
        path = self.game / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def test_running_public_bundle_omits_huddle_and_private_configuration(self):
        b = data.bundle("fixture", public_only=True)
        self.assertEqual(b["roles"], {})
        self.assertEqual([t["kind"] for t in b["turns"]], ["propose", "quest"])
        self.assertNotIn("PRIVATE_MARKER", json.dumps(b))
        for field in ("config", "actors", "seats_done"):
            self.assertNotIn(field, b["status"])
        self.assertNotIn("beliefs", b["turns"][0]["seats"]["P1"]["submission"])
        self.assertIsNone(b["turns"][1]["seats"]["P4"]["submission"])
        self.assertNotIn("memory_history", b)

    def test_finished_public_bundle_releases_final_roles_but_not_private_moves(self):
        self.status["status"] = "finished"
        self.write("referee/status.json", self.status)
        b = data.bundle("fixture", public_only=True)
        self.assertEqual(b["roles"], self.roles)
        self.assertNotIn("PRIVATE_MARKER", json.dumps(b))
        self.assertNotIn("evil_huddle", [t["kind"] for t in b["turns"]])

    def test_memory_uses_committed_receipt_not_later_mutable_file(self):
        self.write("referee/pending/0001/P1.json", {"memory": "EARLY_PRIVATE_MEMORY"})
        path = self.game / "players/p1/work/memory.md"
        path.parent.mkdir(parents=True)
        path.write_text("LATER_PRIVATE_MEMORY")
        h = data.memory_history(self.game, list(self.roles), self.turns[:1])
        self.assertEqual(h["P1"]["1"], "EARLY_PRIVATE_MEMORY")
        self.assertNotIn("LATER_PRIVATE_MEMORY", json.dumps(h))


if __name__ == "__main__": unittest.main(verbosity=2)
