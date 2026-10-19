"""Live referee dashboard (stdlib HTTP, binds to 127.0.0.1 only).

Players cannot reach it: their Landlock policy only permits TCP connects to the
API port, so this server (and anything else on localhost) is out of bounds.

`--public` serves spectators of a tournament instead (put it behind Caddy or a Cloudflare Tunnel):
only tournament games, public-only data while a game runs, no terminals, transcripts, logs or match
launcher, and team prompts only after the configured reveal. See avalon/tournament/publish.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import data
from ..stats import stats_by_kind

STATIC = Path(__file__).resolve().parent / "static"
ROOT = Path(__file__).resolve().parents[2]


def repo_bots() -> list[dict]:
    """Bots the launcher may seat: the built-ins and the .py files in the repo's bots/ folder."""
    files = sorted(p.name for p in (ROOT / "bots").glob("*.py") if not p.name.startswith("_"))
    return [{"key": "scripted", "label": "scripted"}, {"key": "random", "label": "random"}] + \
           [{"key": f"bots/{f}", "label": f[:-3].removesuffix("_bot")} for f in files]
SKELETON = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            '<style>[hidden]{display:none!important}body{margin:0}</style></head><body>{body}</body></html>')


def page(embedded: dict | None = None) -> str:
    html = (STATIC / "app.html").read_text()
    # Inline the vector renderer so live pages and single-file replays match.
    board = (STATIC / "board.js").read_text().replace("</script", "<\\/script")
    html = html.replace("/* AVALON_BOARD_RENDERER */", board)
    if embedded is not None:
        blob = json.dumps(embedded, separators=(",", ":")).replace("</", "<\\/")
        html = html.replace('<script id="avalon-data" type="application/json"></script>',
                            f'<script id="avalon-data" type="application/json">{blob}</script>')
    return html


PUBLIC: dict | None = None     # {"tag": series or None, "reveal": "never" | "now" | ISO time} in --public mode


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, default=str).encode(), "application/json")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        parts = [p for p in u.path.split("/") if p]
        if PUBLIC is not None:
            return self.public_get(parts)
        try:
            if not parts:
                return self._send(200, SKELETON.replace("{body}", page()).encode(), "text/html; charset=utf-8")
            if len(parts) == 2 and parts[0] == "chars" and parts[1].endswith(".webp") and "/" not in parts[1]:
                f = STATIC / "chars" / parts[1]
                if f.is_file():
                    return self._send(200, f.read_bytes(), "image/webp")
            if parts == ["api", "games"]:
                return self._json(data.list_games())
            if parts == ["api", "stats"]:
                return self._json(stats_by_kind(tag=q.get("tag") or None))
            if parts == ["api", "leaderboard"]:
                from ..arena import compute_leaderboard
                return self._json(compute_leaderboard(tag=q.get("tag") or None, include_mock=q.get("mock") == "1"))
            if parts == ["api", "models"]:
                from ..models import load_models, preflight
                return self._json([{"key": k, "label": m.label, "maker": m.maker, "color": m.color,
                                    "backend": m.backend, "model": m.model, "problem": preflight(m)}
                                   for k, m in load_models().items()])
            if parts == ["api", "bots"]:
                return self._json(repo_bots())
            if parts == ["api", "standings"]:
                from ..tournament.series import compute_standings, tournament_tags
                return self._json({**compute_standings(q.get("tag") or None), "tags": tournament_tags()})
            if len(parts) >= 3 and parts[:2] == ["api", "game"]:
                gid = parts[2]
                if len(parts) == 3:
                    return self._json(data.bundle(gid))
                if parts[3] == "live":
                    return self._json(data.live(gid))
                if parts[3] == "audio" and len(parts) == 5 and parts[4].endswith(".mp3") and "/" not in parts[4]:
                    f = data.game_dir(gid) / "audio" / parts[4]
                    if f.is_file():
                        self.send_response(200)
                        self.send_header("content-type", "audio/mpeg")
                        self.send_header("content-length", str(f.stat().st_size))
                        self.send_header("cache-control", "max-age=86400")
                        self.end_headers()
                        self.wfile.write(f.read_bytes())
                        return
                if parts[3] == "transcript" and len(parts) == 5:
                    return self._json(data.transcript_items(data.game_dir(gid), parts[4].upper()))
            return self._json({"error": "not found"}, 404)
        except FileNotFoundError:
            return self._json({"error": "unknown game"}, 404)
        except Exception as e:  # surface errors to the page instead of hanging
            return self._json({"error": repr(e)}, 500)


    def public_get(self, parts):
        """Spectator routes: the page, character art, tournament games as public bundles, standings."""
        from ..tournament import publish
        tag, reveal = PUBLIC["tag"], publish.reveal_due(PUBLIC["reveal"])
        try:
            if not parts:
                body = "<script>window.AVALON_PUBLIC = 1</script>" + page()
                return self._send(200, SKELETON.replace("{body}", body).encode(), "text/html; charset=utf-8")
            if len(parts) == 2 and parts[0] == "chars" and parts[1].endswith(".webp") and "/" not in parts[1]:
                f = STATIC / "chars" / parts[1]
                if f.is_file():
                    return self._send(200, f.read_bytes(), "image/webp")
            if parts == ["api", "games"]:
                return self._json(publish.public_games(tag))
            if parts == ["api", "standings"]:
                return self._json(publish.public_standings(tag, reveal_prompts=reveal))
            if len(parts) in (3, 4) and parts[:2] == ["api", "game"] and \
                    parts[2] in {g["id"] for g in publish.public_games(tag)}:
                if len(parts) == 3:
                    return self._json(publish.public_bundle(parts[2], reveal_prompts=reveal))
                if parts[3] == "live":
                    lv = data.live(parts[2])
                    return self._json({"status": {k: v for k, v in lv["status"].items() if k in publish.VISIBLE_STATUS},
                                       "turns_count": lv["turns_count"], "audio": lv["audio"]})
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": type(e).__name__}, 500)

    def do_POST(self):
        """Start an arena series. Only same-origin page requests, only configured model names."""
        u = urllib.parse.urlparse(self.path)
        if PUBLIC is not None:
            return self._json({"error": "forbidden"}, 403)
        origin = self.headers.get("origin")
        if u.path != "/api/arena":
            return self._json({"error": "not found"}, 404)
        if self.headers.get("x-avalon") != "1" or (origin and urllib.parse.urlparse(origin).netloc != self.headers.get("host")):
            return self._json({"error": "forbidden"}, 403)
        if self.client_address[0] not in ("127.0.0.1", "::1"):     # matches spend model usage: this machine only
            return self._json({"error": "matches can only be started from this machine (or an SSH tunnel)"}, 403)
        try:
            req = json.loads(self.rfile.read(min(int(self.headers.get("content-length") or 0), 65536)) or b"{}")
            from ..models import load_models, preflight
            models = load_models()
            bots = {b["key"] for b in repo_bots()}
            keys = [k for k in req.get("models", []) if k in models or k in bots]
            players, games, parallel = int(req.get("players", 6)), int(req.get("games", 1)), int(req.get("parallel", 1))
            mock = bool(req.get("mock"))
            if not keys or len(keys) != len(req.get("models", [])) or not 5 <= players <= 10 or not 1 <= games <= 50 \
                    or not 1 <= parallel <= 8:
                return self._json({"error": "choose configured models or repo bots, 5-10 players, 1-50 games, 1-8 at once"}, 400)
            problems = [p for k in keys if k in models and (p := preflight(models[k]))] if not mock else []
            if problems:
                return self._json({"error": "; ".join(problems)}, 400)
            tag = f"ui-{time.strftime('%Y%m%d-%H%M%S')}"
            root = ROOT
            argv = [sys.executable, "-m", "avalon.cli", "arena", "--models", ",".join(keys), "--players", str(players),
                    "--games", str(games), "--parallel", str(parallel), "--tag", tag, "--max-rounds", str(min(10, max(1, int(req.get("rounds", 3))))),
                    "--huddle-rounds", str(min(10, max(0, int(req.get("huddle_rounds", 1)))))]
            if mock:
                argv.append("--mock")
            data.GAMES.mkdir(exist_ok=True)
            log = open(data.GAMES / f".{tag}.log", "a")
            subprocess.Popen(argv, cwd=root, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                             env={**os.environ, "PYTHONUNBUFFERED": "1", "AVALON_CWD": str(root)})
            return self._json({"ok": True, "tag": tag})
        except (ValueError, TypeError) as e:
            return self._json({"error": repr(e)}, 400)


def serve(host: str = "127.0.0.1", port: int = 8777, public: dict | None = None):
    global PUBLIC
    PUBLIC = public
    if public is None and host not in ("127.0.0.1", "localhost", "::1"):
        print("warning: the dashboard shows every secret role and private note; bind it to localhost")
    if public is not None:
        print(f"public spectator mode: {'series ' + public['tag'] if public.get('tag') else 'all tournament games'}; "
              f"team prompts revealed: {public.get('reveal') or 'never'}", flush=True)
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    print(f"Avalon dashboard on http://{host}:{port}", flush=True)
    srv.serve_forever()
