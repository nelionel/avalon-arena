"""Check the exported SVG board against actual engine transitions, without model calls.

Run with .tools/venv/bin/python tests/test_board_ui.py.
Uses a fresh headless browser and in-memory fixtures; does not modify saved games.
"""

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from avalon.dashboard.server import SKELETON, STATIC, page
from avalon.engine import Game
from avalon.roles import PRESETS, ROLES, TEAM_SIZES, fails_required

CHROME = os.environ.get("AVALON_BROWSER_EXECUTABLE", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


class Fixture:
    def __init__(self, n):
        self.game = Game({f"P{i + 1}": r for i, r in enumerate(PRESETS[n])}, first_leader="P1")
        self.events = []
        self.turn = 0
        self.apply({p: {"ack": True} for p in self.game.players})

    def apply(self, actions):
        self.turn += 1
        self.events += [dict(e, turn=self.turn) for e in self.game.apply(actions) if e["visibility"] == "public"]

    def proposal(self, approve):
        g = self.game
        team = [g.evil_players[0]] + [p for p in g.players if g.side(p) == "good"][:g.team_size() - 1]
        self.apply({g.leader: {"team": team, "message": None}})
        self.apply({p: {"message": None, "ready_to_vote": True} for p in g.players})
        self.apply({p: {"vote": "approve" if approve else "reject"} for p in g.players})

    def quest(self, fail=False):
        self.proposal(True)
        g = self.game
        self.apply({p: {"card": "fail" if fail and p == g.evil_players[0] else "success"} for p in g.current.team})

    def html(self):
        g = self.game
        b = {"id": "board-fixture", "players": g.players, "public_only": True,
             "status": {"driver": "codex", "model": "gpt-6-luna", "status": "finished"},
             "info": {"quest_team_sizes": g.sizes, "fails_required": [fails_required(g.n, i) for i in range(5)]},
             "roles": g.assignment if g.winner else {}, "public_events": self.events, "events": self.events,
             "turns": [], "commits": [], "rules": "", "audio": None}
        return SKELETON.replace("{body}", page(b))


with sync_playwright() as pw:
    browser = pw.chromium.launch(executable_path=CHROME, args=["--no-sandbox"] if os.geteuid() == 0 else [])
    tab = browser.new_page(viewport={"width": 1440, "height": 1080})
    errors = []
    tab.on("pageerror", lambda e: errors.append(str(e)))
    current = {"html": ""}

    def route(r):
        if "/chars/" in r.request.url:
            p = STATIC / "chars" / r.request.url.rsplit("/", 1)[1]
            r.fulfill(body=p.read_bytes(), content_type="image/webp")
        elif r.request.url.startswith("https://avalon.test/"):
            r.fulfill(body=current["html"], content_type="text/html")
        else:
            r.abort()

    tab.route("**/*", route)

    def check(f, label, rejected, active):
        current["html"] = f.html()
        tab.goto("https://avalon.test/", wait_until="domcontentloaded")
        tab.wait_for_selector(".board-art svg")
        actual = tab.evaluate("""() => ({
          missions: [...document.querySelectorAll('[data-mission]')].map(x => x.getAttribute('aria-label')),
          rejected: document.querySelectorAll('#rejection-tracker [data-filled="true"]').length,
          twoFails: !!document.querySelector('#mission-4 path[fill="#64765E"]'),
          overflow: document.documentElement.scrollWidth > innerWidth,
          seats: document.querySelectorAll('.seatn').length,
          description: document.querySelector('.board-art desc').textContent
        })""")
        expected = []
        for i, size in enumerate(f.game.sizes):
            s = f"Mission {i+1}, {size} agents" + (", current" if active == i+1 else "")
            if i < len(f.game.results):
                s += ", success" if f.game.results[i].success else ", fail"
            expected.append(s)
        assert actual["missions"] == expected, (label, actual, expected)
        assert actual["rejected"] == rejected, (label, actual)
        assert actual["twoFails"] == (f.game.n >= 7), (label, actual)
        assert actual["seats"] == f.game.n and not actual["overflow"], (label, actual)
        assert not errors, errors
        print("PASS", label)

    for n in range(5, 11):
        f = Fixture(n)
        check(f, f"{n} players: initial board, correct sizes/fail threshold", 0, 1)
        for count in range(1, 6):
            f.proposal(False)
            check(f, f"{n} players: {count} rejections", count, 1 if count < 5 else None)
        f = Fixture(n)
        f.proposal(False)
        f.quest()
        check(f, f"{n} players: quest result resets rejection track", 0, 2)
        f.quest(fail=True)
        f.quest()
        check(f, f"{n} players: fourth quest active", 0, 4)
        f.quest()
        check(f, f"{n} players: assassination has no active mission", 0, None)
        for width in (390, 320):
            tab.set_viewport_size({"width": width, "height": 1100})
            check(f, f"{n} players: mobile {width}px", 0, None)
        tab.set_viewport_size({"width": 1440, "height": 1080})
    browser.close()
    print("66 board checks passed; exported renderer requires no external JS or board asset fetches.")
