"""Export one game as a single self-contained HTML page (data embedded)."""

from __future__ import annotations

from pathlib import Path
import base64
import re
import time

from . import data
from .server import SKELETON, page
from ..stats import stats_by_kind

MAX_ITEM = 1800


def _trim(items):
    for it in items:
        for k in ("text", "result"):
            v = it.get(k)
            if isinstance(v, str) and len(v) > MAX_ITEM:
                it[k] = v[:MAX_ITEM] + f"\n...[{len(v) - MAX_ITEM} more chars]"
        if it.get("kind") == "tool":
            it["input"] = {k: (v[:MAX_ITEM] if isinstance(v, str) else v) for k, v in (it.get("input") or {}).items()}
    return items


def export_game(gid: str, out: str | None = None, *, public_only: bool = False, full_document: bool = True) -> str:
    b = data.bundle(gid, include_transcripts=not public_only, public_only=public_only)
    if b.get("transcripts"):
        b["transcripts"] = {p: _trim(v) for p, v in b["transcripts"].items()}
    b["stats"] = None if public_only else stats_by_kind(game_id=gid)
    b["games_index"] = [g for g in data.list_games() if g["id"] == gid]
    chars = Path(__file__).resolve().parent / "static" / "chars"
    b["character_assets"] = {f.name: "data:image/webp;base64," + base64.b64encode(f.read_bytes()).decode()
                             for f in sorted(chars.glob("*.webp"))}
    audio = b.get("audio")
    if audio and audio.get("lines"):
        names = {meta.get("pack") or name for name, meta in audio["lines"].items()}
        adir = data.game_dir(gid) / "audio"
        audio["embedded"] = {name: "data:audio/mpeg;base64," + base64.b64encode((adir / name).read_bytes()).decode()
                             for name in sorted(names)}
    html = page(b)
    # Keep exports usable offline; the CSS already includes local fallback fonts.
    html = re.sub(r'<link\b[^>]*href="https://fonts\.[^"]*"[^>]*>\n?', "", html)
    if full_document:
        html = SKELETON.replace("{body}", html)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = Path(out) if out else data.game_dir(gid) / f"dashboard{'_public' if public_only else ''}_{stamp}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as f:
        f.write(html)
    return str(path)


SITE = Path(__file__).resolve().parents[2] / "site"


def default_site_games() -> list[str]:
    games = data.list_games()
    model_games = [g["id"] for g in games if g["driver"] in ("claude", "codex") and g["status"] in ("finished", "stopped")]
    scripted = [g["id"] for g in games if g["driver"] in ("scripted", "bots") and g["status"] == "finished"][:2]
    return model_games + scripted


def export_site(featured: str | None = None, games: list[str] | None = None, out_dir: Path | None = None) -> dict:
    """Write a multi-game site and return {published path: local file} for the Artifact `files` map.

    The page embeds the featured game; every other game is a data/<id>.json file
    fetched on demand, and voice lines live under audio/<id>/.
    """
    out = Path(out_dir or SITE)
    (out / "data").mkdir(parents=True, exist_ok=True)
    games = games or default_site_games()
    featured = featured or games[0]
    index = [g for g in data.list_games() if g["id"] in games]
    for g in index:
        g["has_audio"] = bool((data.game_dir(g["id"]) / "audio" / "index.json").exists())
    files: dict[str, str] = {}
    stats = stats_by_kind()
    for gid in games:
        b = data.bundle(gid, include_transcripts=True)
        if b.get("transcripts"):
            b["transcripts"] = {p: _trim(v) for p, v in b["transcripts"].items()}
        b["games_index"] = index
        if gid == featured:
            b["stats"] = stats
            (out / "index.html").write_text(page(b))
        else:
            import json
            path = out / "data" / f"{gid}.json"
            path.write_text(json.dumps(b, separators=(",", ":")))
            files[f"data/{gid}.json"] = str(path)
        adir = data.game_dir(gid) / "audio"
        if (adir / "index.json").exists():
            from ..voice import pack
            pack(gid)
            b["audio"] = data.audio_index(data.game_dir(gid))
            if gid == featured:
                (out / "index.html").write_text(page(b))
            else:
                import json
                (out / "data" / f"{gid}.json").write_text(json.dumps(b, separators=(",", ":")))
        packed = [adir / name for name in sorted(set((b.get("audio") or {}).get("packs", {}).values()))]
        for f in packed or sorted(adir.glob("*.mp3")):      # prefer one file per chapter
            files[f"audio/{gid}/{f.name}"] = str(f)
    for f in sorted((Path(__file__).resolve().parent / "static" / "chars").glob("*.webp")):
        files[f"chars/{f.name}"] = str(f)
    return {"page": str(out / "index.html"), "files": files}
