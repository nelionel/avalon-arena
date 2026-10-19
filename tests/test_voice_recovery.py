"""Exercise audio recovery with tiny synthetic clips; no model weights or API calls."""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from avalon import voice


class VoiceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="avalon-voice-test-")
        self.root = Path(self.tmp.name)
        self.game = self.root / "fixture"
        for name in ("public", "referee", "audio"):
            (self.game / name).mkdir(parents=True)
        events = [{"type": "proposal", "turn": i, "leader": f"P{i}", "message": f"Explicit synthetic test utterance {i}."}
                  for i in (1, 2)]
        (self.game / "public/events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
        (self.game / "referee/turns.jsonl").write_text('{"turn":1,"quest":1}\n{"turn":2,"quest":1}\n')
        self.patcher = patch.object(voice, "GAMES", self.root)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def recordings(self):
        for u in voice.spoken_lines(self.game):
            voice._encode_new_mp3(self.game / "audio", u["file"],
                                  ["-f", "lavfi", "-i", "sine=frequency=220:duration=0.1"])

    def index(self): return json.loads((self.game / "audio/index.json").read_text())
    def digest(self, name): return hashlib.sha256((self.game / "audio" / name).read_bytes()).hexdigest()

    def test_missing_index_is_recovered_without_resynthesizing(self):
        self.recordings()
        with patch("kokoro_onnx.Kokoro.from_session", side_effect=AssertionError("Must not synthesize")):
            result = voice.render("fixture")
        self.assertEqual(result, {"rendered": 0, "recovered_metadata": 2, "total": 2})
        self.assertTrue(all(m["seconds"] > 0 for m in self.index()["lines"].values()))

    def test_each_completed_clip_survives_a_later_synthesis_failure(self):
        class FakeTTS:
            calls = 0
            def create(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 2: raise RuntimeError("injected synthesis failure")
                return np.zeros(2400, dtype=np.float32), 24000
        fake = FakeTTS()
        with patch("onnxruntime.InferenceSession", return_value=object()), \
             patch("kokoro_onnx.Kokoro.from_session", return_value=fake), \
             patch.object(voice, "espeak_config", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                voice.render("fixture")
            first = next(iter(self.index()["lines"]))
            before = self.digest(first)
            result = voice.render("fixture")
        self.assertEqual(result["rendered"], 1)
        self.assertEqual(result["total"], 2)
        self.assertEqual(self.digest(first), before)
        self.assertEqual(fake.calls, 3)

    def test_pack_reruns_reuse_files_and_changed_packs_preserve_older_files(self):
        self.recordings()
        voice.render("fixture")
        self.assertEqual(voice.pack("fixture", gap=.05)["created"], 1)
        old = self.index()["packs"]["q1"]
        digest = self.digest(old)
        self.assertEqual(voice.pack("fixture", gap=.05)["created"], 0)
        self.assertEqual(voice.pack("fixture", gap=.1)["created"], 1)
        self.assertNotEqual(self.index()["packs"]["q1"], old)
        self.assertEqual(self.digest(old), digest)

    def test_changed_public_text_cannot_silently_reuse_old_recording(self):
        self.recordings()
        voice.render("fixture")
        p = self.game / "public/events.jsonl"
        p.write_text(p.read_text().replace("utterance 1", "different utterance 1"))
        with self.assertRaisesRegex(RuntimeError, "conflicts"):
            voice.render("fixture")


if __name__ == "__main__": unittest.main(verbosity=2)
