"""The public game repo: a git repository only the referee writes to.

One commit per turn barrier. Players read it (read-only) at every turn.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .persistence import atomic_text


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "referee", "GIT_AUTHOR_EMAIL": "referee@avalon.local",
           "GIT_COMMITTER_NAME": "referee", "GIT_COMMITTER_EMAIL": "referee@avalon.local"}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=env).stdout


def render_event(ev: dict) -> str:
    t = ev["type"]
    if t == "game_started":
        return f"**The game begins.** First leader: **{ev['leader']}**. Quest 1 needs a team of {ev['team_size']}."
    if t == "proposal":
        s = (f"**Quest {ev['quest']}, proposal {ev['proposal']}: leader {ev['leader']} proposes "
             f"{', '.join(ev['team'])}.**")
        if ev.get("message"):
            s += f"\n\n> **{ev['leader']} (pitch):** " + ev["message"].replace("\n", "\n> ")
        return s
    if t == "discussion":
        lines = [f"**Discussion - quest {ev['quest']}, proposal {ev['proposal']}, round {ev['round']}** "
                 "(all messages were submitted simultaneously)", ""]
        for e in ev["entries"]:
            tag = " *(ready to vote)*" if e["ready"] else ""
            if e["message"]:
                lines.append(f"> **{e['player']}**{tag}: " + e["message"].replace("\n", "\n> "))
                lines.append("")
            else:
                lines.append(f"- {e['player']} passed{tag}.")
        return "\n".join(lines).rstrip()
    if t == "team_amended":
        return f"**Leader {ev['leader']} changed the proposed team to {', '.join(ev['team'])}.**"
    if t == "discussion_closed":
        why = {"all_ready": "everyone is ready to vote", "silence": "nobody spoke this round",
               "max_rounds": "the round limit was reached"}[ev["reason"]]
        return f"Discussion closed after {ev['rounds']} round(s): {why}."
    if t == "vote_result":
        approve = [p for p, v in ev["votes"].items() if v == "approve"]
        reject = [p for p, v in ev["votes"].items() if v == "reject"]
        verdict = "APPROVED" if ev["approved"] else "REJECTED"
        return (f"**Vote on {', '.join(ev['team'])} (leader {ev['leader']}): {verdict}** "
                f"({ev['approvals']}/{len(ev['votes'])} approve)\n\n"
                f"- Approve: {', '.join(approve) or '-'}\n- Reject: {', '.join(reject) or '-'}")
    if t == "quest_result":
        res = "SUCCEEDED" if ev["success"] else "FAILED"
        return (f"**Quest {ev['quest']} {res}.** Team: {', '.join(ev['team'])}. "
                f"Cards: {ev['successes']} success, {ev['fails']} fail "
                f"(needed {ev['fails_required']} to fail). Score: Good {ev['score']['good']} - Evil {ev['score']['evil']}.")
    if t == "assassination_phase":
        return f"**{ev['note']}**"
    if t == "assassination":
        s = f"**The Assassin ({ev['assassin']}) names {ev['target']} as Merlin.**"
        if ev.get("message"):
            s += f"\n\n> **{ev['assassin']}:** " + ev["message"].replace("\n", "\n> ")
        return s + (" They were right." if ev["hit"] else " They were wrong.")
    if t == "game_over":
        reason = {"five_rejections": "five proposals were rejected in one quest",
                  "three_failed_quests": "three quests failed",
                  "three_successful_quests": "three quests succeeded",
                  "merlin_assassinated": "the Assassin found Merlin",
                  "merlin_survived": "three quests succeeded and Merlin survived the Assassin"}[ev["reason"]]
        roles = "\n".join(f"- {p}: {r}" for p, r in sorted(ev["roles"].items(), key=lambda x: int(x[0][1:])))
        return f"## GAME OVER - {ev['winner'].upper()} WINS ({reason})\n\nRoles:\n{roles}"
    if t == "reflections":
        lines = ["**Post-game messages**", ""]
        for e in ev["entries"]:
            if e["message"]:
                lines.append(f"> **{e['player']}:** " + e["message"].replace("\n", "\n> "))
                lines.append("")
        return "\n".join(lines).rstrip()
    return f"`{json.dumps(ev)}`"


class PublicRepo:
    def __init__(self, path: Path):
        self.path = Path(path)

    def init(self, rules: str, game_info: dict, state: dict):
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / "updates").mkdir(exist_ok=True)
        _git(self.path, "init", "-q", "-b", "main")
        (self.path / "RULES.md").write_text(rules)
        (self.path / "game.json").write_text(json.dumps(game_info, indent=2) + "\n")
        (self.path / "log.md").write_text(f"# Public log - game {game_info['game_id']}\n")
        (self.path / "events.jsonl").write_text("")
        self.write_state(state)
        (self.path / "README.md").write_text(
            "# Public game repo\n\nRead `RULES.md` first. `state.json` is the current situation, "
            "`updates/NNNN.md` is what each turn produced, `log.md` is the whole story.\n")
        self.commit("setup: rules, players, initial state")

    def write_state(self, state: dict):
        (self.path / "state.json").write_text(json.dumps(state, indent=2) + "\n")

    def publish(self, turn: int, kind: str, events: list[dict], state: dict) -> str:
        public = [e for e in events if e.get("visibility") == "public"]
        body = "\n\n".join(render_event(e) for e in public) or "_No public events._"
        update = f"# Update {turn:04d} ({kind.replace('_', ' ')})\n\n{body}\n"
        subject = f"turn {turn:04d}: {kind}"
        commits = [line.split("\t", 1)[0] for line in _git(self.path, "log", "--format=%H%x09%s").splitlines()
                   if line.split("\t", 1)[-1] == subject]
        if len(commits) > 1:
            raise RuntimeError(f"Duplicate public commits for turn {turn}")
        if commits:
            commit = commits[0]
            stored = json.loads(_git(self.path, "show", f"{commit}:state.json"))
            if stored != state or _git(self.path, "show", f"{commit}:updates/{turn:04d}.md") != update:
                raise RuntimeError(f"Public turn {turn} conflicts with its saved transaction")
            return _git(self.path, "rev-parse", "--short", commit).strip()
        previous_state = json.loads(_git(self.path, "show", "HEAD:state.json"))
        if previous_state.get("next_turn") != turn:
            raise RuntimeError(f"Public history is not ready for turn {turn}")
        # Recover an interrupted write from the committed base, never by appending twice.
        previous_log = _git(self.path, "show", "HEAD:log.md")
        previous_events = _git(self.path, "show", "HEAD:events.jsonl")
        new_events = "".join(json.dumps({"turn": turn, **{k: v for k, v in e.items() if k != "visibility"}})
                             + "\n" for e in public)
        files = {
            f"updates/{turn:04d}.md": update,
            "log.md": previous_log + f"\n## Turn {turn:04d} - {kind.replace('_', ' ')}\n\n{body}\n",
            "events.jsonl": previous_events + new_events,
            "state.json": json.dumps(state, indent=2) + "\n",
        }
        for name, text in files.items():
            atomic_text(self.path / name, text, mode=0o644)
        # Explicit paths keep abandoned temporary files out of the public history.
        _git(self.path, "add", "--", *files)
        _git(self.path, "commit", "-q", "--allow-empty", "-m", subject)
        return _git(self.path, "rev-parse", "--short", "HEAD").strip()

    def commit(self, msg: str) -> str:
        _git(self.path, "add", "-A")
        _git(self.path, "commit", "-q", "--allow-empty", "-m", msg)
        return _git(self.path, "rev-parse", "--short", "HEAD").strip()
