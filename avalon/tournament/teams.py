"""Team submissions: a folder with player.md (the team's system prompt) and an optional team.toml.

  teams/<team>/player.md     at most 1,500 words
  teams/<team>/team.toml     name = "Display name", color = "#3b82f6"   (both optional)

The folder name is the team's id. Folders starting with "_" or "." (such as teams/_baselines) are not teams.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ..models import ROOT

PROMPT_WORDS = 1500
MEMORY_WORDS = 800
BASELINES = ROOT / "teams" / "_baselines"
ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")
PALETTE = ["#e4572e", "#17bebb", "#ffc914", "#76b041", "#7768ae", "#e86a92", "#3a86ff", "#8d6a4f",
           "#2ec4b6", "#ff9f1c"]


class TeamError(ValueError):
    """A submission that cannot play."""


def words(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


@dataclass
class Team:
    id: str
    path: Path
    name: str
    color: str
    prompt: str

    @property
    def sha(self) -> str:
        return hashlib.sha256(self.prompt.encode()).hexdigest()[:12]

    def meta(self) -> dict:
        return {"id": self.id, "name": self.name, "color": self.color, "sha": self.sha}


def check_team(path: str | Path) -> tuple[list[str], list[str]]:
    """(problems, warnings) for one submission folder. Problems stop it from playing."""
    path = Path(path)
    problems, warnings = [], []
    if not path.is_dir():
        return [f"{path}: not a folder"], []
    if not ID_RE.fullmatch(path.name):
        problems.append(f"folder name {path.name!r}: use 1-40 lowercase letters, digits, '-' or '_' (it is the team id)")
    prompt = path / "player.md"
    if not prompt.is_file():
        problems.append("player.md is missing")
    else:
        try:
            text = prompt.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            problems.append("player.md is not UTF-8 text")
        else:
            n = words(text)
            if not text.strip():
                problems.append("player.md is empty")
            elif n > PROMPT_WORDS:
                problems.append(f"player.md has {n} words; the cap is {PROMPT_WORDS}")
    toml = path / "team.toml"
    if toml.exists():
        try:
            meta = tomllib.loads(toml.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
            problems.append(f"team.toml does not parse: {e}")
        else:
            extra = sorted(set(meta) - {"name", "color"})
            if extra:
                warnings.append(f"team.toml: ignoring {', '.join(extra)} (only name and color are read)")
            name = meta.get("name")
            if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 40):
                problems.append("team.toml: name must be 1-40 characters")
            color = meta.get("color")
            if color is not None and not (isinstance(color, str) and COLOR_RE.fullmatch(color)):
                problems.append('team.toml: color must look like "#3b82f6"')
    others = sorted(p.name for p in path.iterdir() if p.name not in ("player.md", "team.toml") and not p.name.startswith("."))
    if others:
        warnings.append(f"ignored: {', '.join(others)} (a submission is player.md and team.toml only)")
    return problems, warnings


def load_team(path: str | Path) -> Team:
    path = Path(path).resolve()
    problems, _ = check_team(path)
    if problems:
        raise TeamError(f"team {path.name}: " + "; ".join(problems))
    meta = tomllib.loads((path / "team.toml").read_text()) if (path / "team.toml").exists() else {}
    color = meta.get("color") or PALETTE[int(hashlib.sha256(path.name.encode()).hexdigest(), 16) % len(PALETTE)]
    return Team(id=path.name, path=path, name=(meta.get("name") or path.name).strip(), color=color,
                prompt=(path / "player.md").read_text(encoding="utf-8").strip())


def discover(teams_dir: str | Path) -> list[Path]:
    """Team folders under teams_dir (or teams_dir itself when it is one submission)."""
    d = Path(teams_dir)
    if (d / "player.md").exists():
        return [d.resolve()]
    if not d.is_dir():
        raise TeamError(f"{d}: no such folder")
    return sorted(p.resolve() for p in d.iterdir() if p.is_dir() and not p.name.startswith(("_", ".")))


def load_teams(teams_dir: str | Path) -> list[Team]:
    teams = [load_team(p) for p in discover(teams_dir)]
    names = [t.name for t in teams]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise TeamError(f"two teams share the display name {', '.join(dupes)}; names must be unique")
    return teams


def demo(out: str | Path, count: int = 8) -> list[Path]:
    """Copy the baselines into `count` team folders (round robin): a field for dry runs."""
    out = Path(out)
    base = sorted(p for p in BASELINES.iterdir() if (p / "player.md").exists())
    made = []
    for i in range(count):
        src = base[i % len(base)]
        dst = out / f"{src.name}-{i // len(base) + 1}"
        if dst.exists():
            raise TeamError(f"{dst} already exists")
        shutil.copytree(src, dst)
        meta = tomllib.loads((src / "team.toml").read_text()) if (src / "team.toml").exists() else {}
        (dst / "team.toml").write_text(f'name = "{meta.get("name", src.name)} {i // len(base) + 1}"\n'
                                       f'color = "{PALETTE[i % len(PALETTE)]}"\n')
        made.append(dst)
    return made
