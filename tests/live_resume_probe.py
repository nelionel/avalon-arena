"""Explicit real-model recovery probe for a STOPPED test game at a discussion turn.

Starts its own referee, kills only that child after some (not all) receipts land,
then resumes to the end of that round. Never targets an existing live referee.
Usage: python3 tests/live_resume_probe.py GAME_ID
"""

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
gid = sys.argv[1]
if Path(gid).name != gid: raise SystemExit("Invalid game ID")
game = ROOT / "games" / gid
ref = game / "referee"
initial = json.loads((ref / "status.json").read_text())
assert initial["status"] == "stopped" and initial["driver"] == "codex" and initial["phase"] == "discuss"
target = initial["turn"] + 1
sessions = {p.name: json.loads((p / "audit/session.json").read_text()) for p in (game / "players").iterdir()}
assert all(not x["ephemeral"] and x["model"] == initial["model"] for x in sessions.values())
stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
out = ROOT.parent / "verification" / ("live-resume-" + stamp)
out.mkdir(parents=True, exist_ok=False)


def launch(name):
    log = open(out / (name + ".log"), "w")
    proc = subprocess.Popen([sys.executable, "-B", "-m", "avalon.cli", "resume", gid,
                             "--max-turns", str(target)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    log.close()
    return proc


def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()


proc = launch("before-interruption")
print(f"Started owned test referee PID {proc.pid}; waiting for a partial round", flush=True)
end = time.monotonic() + 420
receipts = {}
while time.monotonic() < end:
    if proc.poll() is not None:
        raise SystemExit(f"Referee exited before interruption ({proc.returncode}); inspect {out}")
    files = list((ref / "pending" / f"{target:04d}").glob("P*.json"))
    if 0 < len(files) < initial["config"]["players"]:
        current = json.loads((ref / "status.json").read_text())
        assert current["referee_pid"] == proc.pid
        receipts = {p.name: digest(p) for p in files}
        proc.kill()
        proc.wait(timeout=15)
        print(f"Killed only owned test referee after {len(receipts)} accepted moves in turn {target}", flush=True)
        break
    time.sleep(.5)
else:
    proc.terminate()
    proc.wait(timeout=30)
    raise SystemExit("No partial round appeared before timeout; no recovery claim")

public_turns_before = subprocess.check_output(["git", "-C", str(game / "public"), "log", "--format=%s"], text=True).splitlines()
assert not any(s.startswith(f"turn {target:04d}:") for s in public_turns_before)
second = launch("after-interruption")
print(f"Resuming with owned referee PID {second.pid}", flush=True)
try:
    code = second.wait(timeout=480)
except subprocess.TimeoutExpired:
    second.terminate()
    second.wait(timeout=30)
    raise SystemExit(f"Resume timed out; inspect {out}")
assert code == 0, (code, str(out))
final = json.loads((ref / "status.json").read_text())
assert final["status"] == "stopped" and final["turn"] == target
for name, h in receipts.items():
    assert digest(ref / "pending" / f"{target:04d}" / name) == h
after_sessions = {p.name: json.loads((p / "audit/session.json").read_text()) for p in (game / "players").iterdir()}
assert sessions == after_sessions
turns = [json.loads(s) for s in (ref / "turns.jsonl").read_text().splitlines()]
assert len(turns) == target and all(not s["defaulted"] for t in turns for s in t["seats"].values())
public_turns_after = subprocess.check_output(["git", "-C", str(game / "public"), "log", "--format=%s"], text=True).splitlines()
assert sum(s.startswith(f"turn {target:04d}:") for s in public_turns_after) == 1
report = {"game": gid, "model": initial["model"], "target_turn": target, "partial_receipts": receipts,
          "same_persistent_sessions": True, "accepted_moves_unchanged": True,
          "round_unpublished_until_complete": True, "exactly_one_public_commit": True, "no_defaults": True,
          "test": "real model: stopped game resumed, SIGKILL mid-round, then resumed again", "result": "passed"}
(out / "result.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report), flush=True)
print("Evidence:", out, flush=True)
