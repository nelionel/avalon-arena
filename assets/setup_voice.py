"""Download/verify pinned Kokoro voice assets. Does not install global software."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
manifest = json.loads((ROOT / "assets/voice-models.json").read_text())
dest = ROOT / ".tools/kokoro"
dest.mkdir(parents=True, exist_ok=True)

for item in manifest:
    path = dest / item["name"]
    candidate = path if path.exists() else path.with_suffix(path.suffix + ".part")
    if not path.exists():
        subprocess.run(["curl", "--fail", "--location", "--retry", "3", "--silent", "--show-error",
                        item["url"], "--output", str(candidate)], check=True)
    if candidate.stat().st_size != item["bytes"] or hashlib.sha256(candidate.read_bytes()).hexdigest() != item["sha256"]:
        raise SystemExit(f"Asset verification failed: {candidate}. Existing files were not replaced.")
    if not path.exists():
        os.link(candidate, path)
        candidate.unlink()
    print("Verified", path.name)
