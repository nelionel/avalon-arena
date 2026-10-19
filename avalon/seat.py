"""Provision one player's sandboxed Claude Code environment ("seat").

Layout (owner, mode):

    games/<gid>/                       root 711
      public/                          root 755   the public git repo (read-only to players)
      referee/                         root 700   secrets, env files, tmux socket, audit repos
      players/                         root 711
        p3/                            root 711
          conf/settings.json           root 644   Claude Code settings (permissions + hooks)
          work/                        seat 700   cwd of the player's Claude Code
            CLAUDE.md, ROLE.md         root 644   onboarding (private)
            inbox/                     root 755   turn files written by the referee
            notes/ outbox/ scratch/    seat 700   the player's own output
          home/                        seat 700   HOME + CLAUDE_CONFIG_DIR (transcripts)
          tmp/                         seat 700   TMPDIR
          audit/                       seat 700   hook log (written by the in-jail hook)
"""

from __future__ import annotations

import json
import os
import shlex
import uuid
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "runtime"
HOOKS = ROOT / "hooks"
CLAUDE_BIN = RUNTIME / "claude"
JAIL_BIN = RUNTIME / "avalon-jail"
UID_BASE = 61000
SLOT_STRIDE = 20

BASH_READONLY = ["ls", "cat", "head", "tail", "wc", "grep", "sort", "uniq", "diff",
                 "git log", "git show", "git diff", "date", "pwd"]
CLAUDE_TOOLS_BASE = ["Read", "Write", "Edit", "Glob", "Grep", "TodoWrite"]

# Non-secret environment for every player. Credentials come from a root-only env file.
QUIET_ENV = {
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_ORG_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
    "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    "CLAUDE_CODE_DISABLE_CRON": "1",
    "CLAUDE_CODE_DISABLE_WORKFLOWS": "1",
    "CLAUDE_CODE_DISABLE_ADVISOR_TOOL": "1",
    "CLAUDE_CODE_DISABLE_ARTIFACT": "1",
    "CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS": "1",
    "CLAUDE_CODE_DISABLE_CLAUDE_API_SKILL": "1",
    "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
    "CLAUDE_CODE_DISABLE_MOUSE": "1",
    "CLAUDE_CODE_DISABLE_WEB_FETCH": "1",
}

HOOK_EVENTS_TOOL = ["PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionDenied"]
HOOK_EVENTS_OTHER = ["SessionStart", "UserPromptSubmit", "Stop", "StopFailure", "Notification",
                     "PreCompact", "PostCompact", "SessionEnd", "InstructionsLoaded"]


@dataclass
class SeatConfig:
    model: str = "claude-haiku-5-5"
    effort: str | None = None
    allow_bash: bool = True
    tcp_ports: list[int] = field(default_factory=lambda: [443, 53])
    nice: int = 10
    hook_guard: bool = True


@dataclass
class Seat:
    game_dir: Path
    player: str            # "P3"
    slot: int
    cfg: SeatConfig
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    # -- paths ---------------------------------------------------------------
    @property
    def name(self) -> str:
        return self.player.lower()

    @property
    def uid(self) -> int:
        return UID_BASE + self.slot * SLOT_STRIDE + int(self.player[1:])

    @property
    def dir(self) -> Path:
        return self.game_dir / "players" / self.name

    @property
    def work(self) -> Path:
        return self.dir / "work"

    @property
    def home(self) -> Path:
        return self.dir / "home"

    @property
    def conf(self) -> Path:
        return self.dir / "conf"

    @property
    def audit(self) -> Path:
        return self.dir / "audit"

    @property
    def tmp(self) -> Path:
        return self.dir / "tmp"

    @property
    def public(self) -> Path:
        return self.game_dir / "public"

    @property
    def referee(self) -> Path:
        return self.game_dir / "referee"

    @property
    def env_file(self) -> Path:
        return self.referee / "seats" / f"{self.name}.env"

    @property
    def launch_script(self) -> Path:
        return self.referee / "seats" / f"{self.name}.launch.sh"

    @property
    def hooks_log(self) -> Path:
        return self.audit / "hooks.jsonl"

    @property
    def transcript(self) -> Path | None:
        hits = sorted((self.home / ".claude" / "projects").glob(f"*/{self.session_id}.jsonl"))
        return hits[0] if hits else None

    @property
    def read_roots(self) -> list[str]:
        return [str(self.work), str(self.public)]

    @property
    def write_roots(self) -> list[str]:
        w = self.work
        return [str(w / "notes"), str(w / "outbox"), str(w / "scratch"), str(w / "journal.md"), str(w / "memory.md")]

    # -- provisioning --------------------------------------------------------
    def provision(self, claude_md: str, role_md: str, credentials: dict[str, str]):
        u = self.uid
        for d, owner, mode in [
            (self.dir, 0, 0o711), (self.conf, 0, 0o755), (self.work, u, 0o700),
            (self.work / "inbox", 0, 0o755), (self.work / "notes", u, 0o700),
            (self.work / "outbox", u, 0o700), (self.work / "scratch", u, 0o700),
            (self.home, u, 0o700), (self.home / ".claude", u, 0o700),
            (self.tmp, u, 0o700), (self.audit, u, 0o700), (self.referee / "seats", 0, 0o700),
        ]:
            d.mkdir(parents=True, exist_ok=True)
            os.chown(d, owner, owner)
            os.chmod(d, mode)
        _write(self.work / "CLAUDE.md", claude_md, 0, 0o644)
        _write(self.work / "ROLE.md", role_md, 0, 0o644)
        _write(self.hooks_log, "", u, 0o600)
        _write(self.conf / "settings.json", json.dumps(self.settings(), indent=2) + "\n", 0, 0o644)
        _write(self.home / ".claude" / ".claude.json",
               json.dumps(self.claude_state(credentials), indent=2) + "\n", u, 0o600)
        _write(self.home / ".gitconfig", f"[safe]\n\tdirectory = {self.public}\n", u, 0o600)
        env = "".join(f"{k}={v}\n" for k, v in credentials.items())
        _write(self.env_file, env, 0, 0o600)
        _write(self.launch_script, self.launch_sh(resume=False), 0, 0o700)
        _write(self.launch_script.with_suffix(".resume.sh"), self.launch_sh(resume=True), 0, 0o700)

    def settings(self) -> dict:
        w, pub = str(self.work), str(self.public)
        hook = f"/usr/bin/python3 -I {HOOKS / 'agent_hook.py'}"
        h = [{"hooks": [{"type": "command", "command": hook, "timeout": 30}]}]
        ht = [{"matcher": "*", "hooks": [{"type": "command", "command": hook, "timeout": 30}]}]
        allow = [f"Read(/{w}/**)", f"Read(/{pub}/**)",
                 f"Edit(/{w}/notes/**)", f"Edit(/{w}/outbox/**)", f"Edit(/{w}/scratch/**)",
                 f"Edit(/{w}/journal.md)", f"Edit(/{w}/memory.md)", "TodoWrite"]
        if self.cfg.allow_bash:
            allow += [f"Bash({c})" for c in BASH_READONLY] + [f"Bash({c} *)" for c in BASH_READONLY]
        deny = ["WebFetch", "WebSearch", "Task", "Agent", "NotebookEdit",
                f"Edit(/{w}/inbox/**)", f"Edit(/{w}/ROLE.md)", f"Edit(/{w}/CLAUDE.md)",
                f"Read(/{self.home}/**)", f"Read(/{self.audit}/**)", f"Read(/{self.conf}/**)"]
        if not self.cfg.allow_bash:
            deny.append("Bash")
        return {
            "permissions": {"defaultMode": "dontAsk", "allow": allow, "deny": deny},
            "hooks": {**{e: ht for e in HOOK_EVENTS_TOOL}, **{e: h for e in HOOK_EVENTS_OTHER}},
            "includeCoAuthoredBy": False,
            "spinnerTipsEnabled": False,
            "cleanupPeriodDays": 36500,
        }

    def claude_state(self, credentials) -> dict:
        state = {
            "hasCompletedOnboarding": True,
            "lastOnboardingVersion": "2.1.295",
            "theme": "dark",
            "numStartups": 1,
            "autoUpdates": False,
            "projects": {str(self.work): {"hasTrustDialogAccepted": True,
                                          "hasCompletedProjectOnboarding": True, "allowedTools": []}},
        }
        key = credentials.get("ANTHROPIC_API_KEY")
        if key:
            state["customApiKeyResponses"] = {"approved": [key[-20:]], "rejected": []}
        return state

    def jail_argv(self) -> list[str]:
        a = [str(JAIL_BIN), "--uid", str(self.uid), "--gid", str(self.uid), "--nice", str(self.cfg.nice),
             "--chown-tty", "--env-file", str(self.env_file)]
        for p in ["/usr", "/bin", "/lib", "/lib64", "/sbin", str(RUNTIME)]:
            a += ["--rox", p]
        for p in ["/etc", "/proc", "/sys", str(HOOKS), str(self.public), str(self.conf)]:
            a += ["--ro", p]
        for p in ["/dev/null", "/dev/zero", "/dev/full", "/dev/random", "/dev/urandom", "/dev/tty",
                  "/dev/pts", "/dev/ptmx"]:
            a += ["--dev", p]
        for p in [self.work, self.home, self.tmp, self.audit]:
            a += ["--rw", str(p)]
        for port in self.cfg.tcp_ports:
            a += ["--tcp", str(port)]
        env = {
            "HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.home / ".claude"), "TMPDIR": str(self.tmp),
            "PATH": "/usr/local/bin:/usr/bin:/bin", "TERM": "xterm-256color", "COLORTERM": "truecolor",
            "LANG": "C.UTF-8", "SHELL": "/bin/bash", "USER": self.name, "LOGNAME": self.name,
            "AVALON_SEAT": self.player, "AVALON_WORK": str(self.work), "AVALON_AUDIT": str(self.audit),
            "AVALON_READ_ROOTS": ":".join(self.read_roots), "AVALON_WRITE_ROOTS": ":".join(self.write_roots),
            "AVALON_GUARD": "1" if self.cfg.hook_guard else "0",
            **QUIET_ENV,
        }
        for k, v in env.items():
            a += ["--env", f"{k}={v}"]
        return a + ["--cwd", str(self.work), "--"]

    def claude_argv(self, resume: bool) -> list[str]:
        tools = CLAUDE_TOOLS_BASE + (["Bash"] if self.cfg.allow_bash else [])
        a = [str(CLAUDE_BIN), "--model", self.cfg.model, "--permission-mode", "dontAsk", "--restricted",
             "--tools", ",".join(tools), "--settings", str(self.conf / "settings.json"),
             "--strict-mcp-config", "--add-dir", str(self.public), "--name", f"avalon-{self.name}",
             "--append-system-prompt-file", str(self.work / "CLAUDE.md")]
        a += ["--resume", self.session_id] if resume else ["--session-id", self.session_id]
        if self.cfg.effort:
            a += ["--effort", self.cfg.effort]
        return a

    def launch_sh(self, resume: bool) -> str:
        argv = self.jail_argv() + self.claude_argv(resume)
        return "#!/bin/bash\n# generated by avalon.seat - nothing secret on this command line\nexec " + \
            " \\\n  ".join(shlex.quote(x) for x in argv) + "\n"


class LocalSeat(Seat):
    """A seat for in-process bots: private folders owned by the current user. No root, uid or jail."""

    @property
    def uid(self) -> int:
        return os.getuid()

    def provision(self, claude_md: str, role_md: str, credentials: dict[str, str]):
        for d in [self.dir, self.work, self.work / "inbox", self.work / "notes", self.work / "outbox",
                  self.work / "scratch", self.audit, self.referee / "seats"]:
            d.mkdir(parents=True, exist_ok=True)
            d.chmod(0o700)
        (self.work / "ROLE.md").write_text(role_md)
        (self.work / "memory.md").write_text("# Private player memory\n")
        self.hooks_log.write_text("")
        self.launch_script.write_text("# In-process bot: no separate process, credentials or sandbox.\n")


def _write(path: Path, text: str, owner: int, mode: int):
    path.write_text(text)
    os.chown(path, owner, owner)
    os.chmod(path, mode)
