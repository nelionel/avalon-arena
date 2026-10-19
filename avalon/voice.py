"""Give every player a voice: render public messages to speech with Kokoro.

Kokoro-82M is an open-weights neural TTS (Apache-2.0) running locally on the
CPU through onnxruntime, at low priority and with few threads so it never
competes with other work on this machine. Each seat gets a distinct voice.

Run with the project venv (it has kokoro-onnx):
    .tools/venv/bin/python -m avalon.voice GAME_ID [--turn N]

Output: games/<id>/audio/<turn>_<player>_<kind>.mp3 and audio/index.json.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .persistence import write_json, sync_directory

ROOT = Path(__file__).resolve().parent.parent
GAMES = ROOT / "games"
MODEL = ROOT / ".tools" / "kokoro" / "kokoro-v1.0.onnx"
VOICES = ROOT / ".tools" / "kokoro" / "voices-v1.0.bin"
VENV_PY = ROOT / ".tools" / "venv" / "bin" / "python"


def ffmpeg_executable() -> str:
    installed = shutil.which("ffmpeg")
    if installed:
        return installed
    # Project-local wheels provide a native FFmpeg when the host has none.
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def espeak_config():
    import espeakng_loader
    from kokoro_onnx.config import EspeakConfig
    # The bundled macOS eSpeak library failed with the long site-packages path.
    # An identical copy under the shorter project tools path initializes correctly.
    data = Path(os.environ.get("AVALON_ESPEAK_DATA_PATH", str(ROOT / ".tools" / "espeak-ng-data")))
    if not data.exists():
        shutil.copytree(espeakng_loader.get_data_path(), data)
    for name in ("phontab", "phondata", "phonindex"):
        if not (data / name).is_file():
            raise RuntimeError(f"Incomplete eSpeak data directory: {data} (missing {name})")
    return EspeakConfig(data_path=str(data), lib_path=espeakng_loader.get_library_path())

# Ten clearly different English voices (American / British, female / male).
CAST = ["am_michael", "af_heart", "bm_george", "bf_emma", "am_fenrir",
        "af_nicole", "bm_lewis", "bf_isabella", "am_puck", "af_bella"]
NARRATOR = "bm_fable"


def available() -> bool:
    return MODEL.exists() and VOICES.exists() and VENV_PY.exists()


def voice_for(player: str) -> str:
    return CAST[(int(player[1:]) - 1) % len(CAST)]


def spoken_lines(game_dir: Path) -> list[dict]:
    """Every public utterance in the game, in order."""
    out = []
    for line in (game_dir / "public" / "events.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        t = e["turn"]
        if e["type"] == "proposal" and e.get("message"):
            out.append({"turn": t, "player": e["leader"], "kind": "pitch", "text": e["message"]})
        elif e["type"] == "discussion":
            for x in e["entries"]:
                if x.get("message"):
                    out.append({"turn": t, "player": x["player"], "kind": f"r{e['round']}", "text": x["message"]})
        elif e["type"] == "assassination" and e.get("message"):
            out.append({"turn": t, "player": e["assassin"], "kind": "assassin", "text": e["message"]})
        elif e["type"] == "reflections":
            for x in e["entries"]:
                if x.get("message"):
                    out.append({"turn": t, "player": x["player"], "kind": "reflect", "text": x["message"]})
    for u in out:
        u["file"] = f"{u['turn']:04d}_{u['player']}_{u['kind']}.mp3"
    return out


def clean_for_speech(text: str) -> str:
    text = re.sub(r"[*_`#>]+", " ", text)                    # markdown punctuation
    text = re.sub(r"\bP(\d+)\b", r"P \1", text)               # "P3" -> "P 3" reads naturally
    return re.sub(r"\s+", " ", text).strip()


def _encode_new_mp3(adir: Path, filename: str, input_args: list[str], pcm: bytes | None = None):
    """Publish a complete new MP3 atomically; never overwrite an existing recording."""
    with tempfile.NamedTemporaryFile(prefix=".encoding-", suffix=".part", dir=adir) as tmp:
        subprocess.run([ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-y", *input_args,
                        "-ac", "1", "-codec:a", "libmp3lame", "-b:a", "40k", "-f", "mp3", tmp.name],
                       input=pcm, check=True)
        with open(tmp.name, "rb") as ready:
            os.fsync(ready.fileno())
        os.link(tmp.name, adir / filename)
        sync_directory(adir)


def render(game_id: str, turn: int | None = None, threads: int = 4) -> dict:
    os.environ.setdefault("OMP_NUM_THREADS", str(threads))
    import soundfile as sf

    gdir = GAMES / game_id
    adir = gdir / "audio"
    adir.mkdir(exist_ok=True)
    with open(adir / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        index_path = adir / "index.json"
        index = json.loads(index_path.read_text()) if index_path.exists() else {"cast": {}, "lines": {}}
        lines = [u for u in spoken_lines(gdir) if turn is None or u["turn"] == turn]
        todo = []
        repaired = 0
        for u in lines:
            file = adir / u["file"]
            v = voice_for(u["player"])
            text_hash = hashlib.sha256(u["text"].encode()).hexdigest()
            previous = index["lines"].get(u["file"], {})
            if previous.get("text_sha256", text_hash) != text_hash or previous.get("voice", v) != v:
                raise RuntimeError(f"Recording metadata conflicts with the public utterance: {u['file']}")
            index["cast"][u["player"]] = v
            if file.exists():
                info = sf.info(str(file))
                if info.frames <= 0:
                    raise RuntimeError(f"Existing recording is empty: {file}")
                if not previous:
                    repaired += 1
                    previous = {"turn": u["turn"], "player": u["player"], "kind": u["kind"],
                                "seconds": round(info.duration, 2), "voice": v}
                index["lines"][u["file"]] = {**previous, "text_sha256": text_hash}
            else:
                todo.append(u)
        # Recover recordings left by an interruption before their metadata was saved.
        write_json(index_path, index)
        if todo:
            import onnxruntime as ort
            from kokoro_onnx import Kokoro
            so = ort.SessionOptions()
            so.intra_op_num_threads, so.inter_op_num_threads = threads, 1
            k = Kokoro.from_session(ort.InferenceSession(str(MODEL), so, providers=["CPUExecutionProvider"]), str(VOICES),
                                    espeak_config=espeak_config())
            for u in todo:
                v = voice_for(u["player"])
                lang = "en-gb" if v.startswith("b") else "en-us"
                samples, sr = k.create(clean_for_speech(u["text"]), voice=v, speed=1.05, lang=lang)
                with tempfile.NamedTemporaryFile(suffix=".wav", dir=adir) as wav:
                    sf.write(wav.name, samples, sr)
                    _encode_new_mp3(adir, u["file"], ["-i", wav.name])
                index["lines"][u["file"]] = {"turn": u["turn"], "player": u["player"], "kind": u["kind"],
                    "seconds": round(len(samples) / sr, 2), "voice": v,
                    "text_sha256": hashlib.sha256(u["text"].encode()).hexdigest()}
                write_json(index_path, index)  # every completed clip survives a later failure
        return {"rendered": len(todo), "recovered_metadata": repaired, "total": len(index["lines"])}


def render_async(game_id: str, turn: int | None = None):
    """Fire-and-forget rendering from the referee (system Python, no kokoro)."""
    if not available():
        return None
    args = ["nice", "-n", "19", str(VENV_PY), "-m", "avalon.voice", game_id]
    if turn is not None:
        args += ["--turn", str(turn)]
    log = open(GAMES / game_id / "referee" / "voice.log", "a")
    return subprocess.Popen(args, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("game_id")
    ap.add_argument("--turn", type=int)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--pack", action="store_true", help="also concatenate lines into one MP3 per chapter")
    a = ap.parse_args()
    print(json.dumps(render(a.game_id, a.turn, a.threads)))
    if a.pack:
        print(json.dumps(pack(a.game_id)))


def pack(game_id: str, gap: float = 0.35) -> dict:
    """Create immutable chapter audio, retaining older packs if the chapter grows."""
    gdir = GAMES / game_id
    adir = gdir / "audio"
    with open(adir / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        index_path = adir / "index.json"
        index = json.loads(index_path.read_text())
        quest_of = {}
        for line in (gdir / "referee" / "turns.jsonl").read_text().splitlines():
            if line.strip():
                t = json.loads(line)
                quest_of[t["turn"]] = t.get("quest")
        groups: dict[str, list[str]] = {}
        for f, meta in sorted(index["lines"].items()):
            g = "end" if meta["kind"] in ("assassin", "reflect") else f"q{quest_of.get(meta['turn'], 0)}"
            groups.setdefault(g, []).append(f)
        sr = 24000
        silence = b"\0\0" * int(sr * gap)
        index["packs"] = {}
        created = 0
        for g, files in groups.items():
            fingerprint = hashlib.sha256(json.dumps({"version": 1, "gap": gap, "sample_rate": sr,
                "files": [(f, hashlib.sha256((adir / f).read_bytes()).hexdigest()) for f in files]},
                sort_keys=True).encode()).hexdigest()[:16]
            filename = f"pack_{g}_{fingerprint}.mp3"
            pcm, pos = bytearray(), 0.0
            for f in files:
                raw = subprocess.run([ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-i", str(adir / f),
                    "-f", "s16le", "-ac", "1", "-ar", str(sr), "-"], check=True, capture_output=True).stdout
                dur = len(raw) / 2 / sr
                index["lines"][f].update(pack=filename, start=round(pos, 3), dur=round(dur, 3))
                pcm += raw + silence
                pos += dur + gap
            if not (adir / filename).exists():
                _encode_new_mp3(adir, filename, ["-f", "s16le", "-ar", str(sr), "-ac", "1", "-i", "-"], bytes(pcm))
                created += 1
            index["packs"][g] = filename
        write_json(index_path, index)
        return {"packs": len(index["packs"]), "created": created, "lines": len(index["lines"])}


if __name__ == "__main__":
    sys.exit(main())
