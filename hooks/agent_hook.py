"""Claude Code hook for Avalon players. Runs inside the player's jail.

Every hook event is appended to $AVALON_AUDIT/hooks.jsonl (the referee tails
it). On top of logging it enforces two things:

* PreToolUse path guard: file tools may only read the player's private work
  folder and the public repo, and may only write notes/, outbox/, scratch/
  and journal.md. (Third layer, after Claude Code permission rules and
  Landlock.)
* Stop gate: while a turn is open, the player cannot end its turn until the
  private decision document and a valid submission exist. The hook blocks the
  stop with the exact problems, up to MAX_STOP_BLOCKS times per turn.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import turnspec  # noqa: E402

MAX_STOP_BLOCKS = 3
AUDIT = os.environ.get("AVALON_AUDIT", ".")
WORK = os.environ.get("AVALON_WORK", ".")
SEAT = os.environ.get("AVALON_SEAT", "?")
READ_ROOTS = [r for r in os.environ.get("AVALON_READ_ROOTS", "").split(":") if r]
WRITE_ROOTS = [r for r in os.environ.get("AVALON_WRITE_ROOTS", "").split(":") if r]
GUARD = os.environ.get("AVALON_GUARD", "1") != "0"   # "0" only in layer-isolation tests

FILE_READ_TOOLS = {"Read": "file_path", "Glob": "path", "Grep": "path", "LS": "path"}
FILE_WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path",
                    "NotebookEdit": "notebook_path"}


def log(rec):
    rec = {"ts": round(time.time(), 3), "seat": SEAT, **rec}
    with open(os.path.join(AUDIT, "hooks.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def clip(v, n=400):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + f"...(+{len(s) - n})"


def under(path, roots):
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def resolve(p, cwd):
    if not p:
        return os.path.realpath(cwd)
    return os.path.realpath(os.path.join(cwd, os.path.expanduser(p)))


def guard(tool, inp, cwd):
    """Return (ok, checked_paths, reason)."""
    if tool in FILE_READ_TOOLS:
        paths = [resolve(inp.get(FILE_READ_TOOLS[tool]), cwd)]
        pat = inp.get("pattern") if tool == "Glob" else None
        if pat and (pat.startswith("/") or ".." in pat.split("/")):
            paths.append(resolve(pat.split("*")[0] or "/", cwd))
        bad = [p for p in paths if not under(p, READ_ROOTS)]
        return (not bad, paths, f"reading outside your folders is not allowed: {bad}" if bad else "")
    if tool in FILE_WRITE_TOOLS:
        p = resolve(inp.get(FILE_WRITE_TOOLS[tool]), cwd)
        ok = under(p, WRITE_ROOTS)
        return (ok, [p], "" if ok else f"you may only write in notes/, outbox/, scratch/ or journal.md: {p}")
    return True, [], ""


def read_public_this_turn(spec) -> bool:
    """Did this seat Read the public state file since the referee opened the turn?"""
    marker = f"inbox/turn_{spec['turn']:04d}.md"
    state = spec["read_first"][0] if spec.get("read_first") else None
    if not state:
        return True
    try:
        with open(os.path.join(AUDIT, "hooks.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(x) for x in f.readlines()[-3000:] if x.strip()]
    except (OSError, json.JSONDecodeError):
        return True
    start = next((i for i, r in enumerate(rows) if r.get("event") == "UserPromptSubmit"
                  and marker in (r.get("prompt") or "")), None)
    if start is None:
        return True
    for r in rows[start:]:
        if r.get("event") != "PreToolUse":
            continue
        if r.get("tool") == "Read" and state in (r.get("paths") or []):
            return True
        if r.get("tool") == "Bash" and state in (r.get("input") or ""):
            return True
    return False


def current_spec():
    try:
        with open(os.path.join(WORK, "inbox", "current.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def bump(name):
    path = os.path.join(AUDIT, name)
    try:
        with open(path) as f:
            n = int(f.read().strip() or 0)
    except (OSError, ValueError):
        n = 0
    with open(path, "w") as f:
        f.write(str(n + 1))
    return n + 1


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        data = {}
    ev = data.get("hook_event_name", "?")
    cwd = data.get("cwd") or WORK

    if ev == "PreToolUse":
        tool, inp = data.get("tool_name", ""), data.get("tool_input") or {}
        ok, paths, reason = guard(tool, inp, cwd)
        if not GUARD:
            ok, reason = True, ""
        log({"event": ev, "tool": tool, "input": clip(inp), "paths": paths, "guard": "allow" if ok else "deny",
             "tool_use_id": data.get("tool_use_id")})
        if not ok:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": f"[avalon-jail] {reason}"}}))
        return

    if ev in ("PostToolUse", "PostToolUseFailure"):
        resp = data.get("tool_response")
        log({"event": ev, "tool": data.get("tool_name"), "tool_use_id": data.get("tool_use_id"),
             "input": clip(data.get("tool_input"), 200), "response": clip(resp, 300),
             "error": clip(data.get("error"), 300) if data.get("error") else None})
        return

    if ev == "Stop":
        spec = current_spec()
        if not spec or not spec.get("open"):
            log({"event": ev, "turn": None, "status": "idle"})
            return
        turn = spec["turn"]
        errs, norm, note_words = turnspec.check_turn_files(spec)
        if not read_public_this_turn(spec):
            errs.insert(0, f"you have not read the public repo this turn: read {spec['read_first'][0]} "
                           "(and the new update files listed in your turn file) before submitting")
        if errs:
            n = bump(f"stop_blocks_{turn:04d}")
            if n <= MAX_STOP_BLOCKS:
                log({"event": ev, "turn": turn, "status": "blocked", "attempt": n, "errors": errs})
                reason = ("[avalon-referee] Your turn is not complete:\n- " + "\n- ".join(errs) +
                          f"\nFix these, then stop. (Turn {turn}, spec in {WORK}/inbox/turn_{turn:04d}.md)")
                print(json.dumps({"decision": "block", "reason": reason}))
                return
            log({"event": "turn_done", "turn": turn, "valid": False, "errors": errs, "note_words": note_words})
            return
        log({"event": "turn_done", "turn": turn, "valid": True, "submission": norm, "note_words": note_words})
        return

    rec = {"event": ev}
    for k in ("source", "prompt", "message", "notification_type", "trigger", "reason", "error",
              "session_id", "transcript_path", "file_path", "memory_type", "load_reason", "tool_name"):
        if data.get(k) is not None:
            rec[k] = clip(data[k], 300)
    if ev == "PermissionDenied":
        rec["input"] = clip(data.get("tool_input"), 300)
    log(rec)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never wedge the player on a hook bug
        try:
            log({"event": "hook_error", "error": repr(e)})
        except Exception:
            pass
