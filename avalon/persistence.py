"""Durable referee records. Private receipts precede every public turn commit."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import tempfile
from pathlib import Path


def sync_directory(path: Path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_text(path: Path, text: str, *, mode=0o600, once=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if once and path.exists():
        if path.read_text() != text:
            raise RuntimeError(f"Conflicting immutable recovery record: {path}")
        return
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    tmp = Path(name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if once:
            os.link(tmp, path)  # create the final name only after its contents are durable
        else:
            os.replace(tmp, path)
        sync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def write_json(path: Path, value, *, once=False):
    atomic_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n", once=once)


def sync_jsonl(path: Path, records):
    """Extend or repair a partially written derived journal; never replace a conflict."""
    expected = "".join(json.dumps(row, sort_keys=True) + "\n" for row in records)
    current = path.read_text() if path.exists() else ""
    if not expected.startswith(current):
        raise RuntimeError(f"Saved journal disagrees with recovery transactions: {path}")
    if current != expected:
        atomic_text(path, expected)


class GameLease:
    def __init__(self, path: Path):
        self.path, self.fh = path, None

    def acquire(self):
        self.fh = open(self.path, "a+")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.fh.close()
            self.fh = None
            raise RuntimeError("This game already has a live referee; refusing a second owner") from None

    def release(self):
        if self.fh:
            self.fh.close()
            self.fh = None


def process_identity(pid: int) -> dict | None:
    """Record both creation time and command, so PID reuse is never ownership."""
    r = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
                       capture_output=True, text=True)
    line = r.stdout.strip()
    return {"pid": pid, "identity": line} if r.returncode == 0 and line else None
