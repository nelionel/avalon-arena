"""Screenshot the dashboard with the project-local headless Chromium.

usage: .tools/venv/bin/python tests/shot.py URL OUT.png [--width 1400] [--height 900]
       [--dark] [--full] [--click SELECTOR]... [--wait MS] [--eval JS]
"""

import argparse
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
CHROME = ROOT / ".tools/browsers/chromium-1134/chrome-linux/chrome"
if os.environ.get("AVALON_BROWSER_EXECUTABLE"):
    CHROME = Path(os.environ["AVALON_BROWSER_EXECUTABLE"])
elif not CHROME.exists():
    CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
NSS = ROOT / ".tools/nss/usr/lib/x86_64-linux-gnu"

ap = argparse.ArgumentParser()
ap.add_argument("url")
ap.add_argument("out")
ap.add_argument("--width", type=int, default=1400)
ap.add_argument("--height", type=int, default=900)
ap.add_argument("--dark", action="store_true")
ap.add_argument("--full", action="store_true")
ap.add_argument("--click", action="append", default=[])
ap.add_argument("--wait", type=int, default=1500)
ap.add_argument("--eval")
a = ap.parse_args()

env = {**os.environ, "LD_LIBRARY_PATH": f"{NSS}:{os.environ.get('LD_LIBRARY_PATH', '')}"}
with sync_playwright() as p:
    args = ["--disable-gpu"]
    if os.geteuid() == 0:
        args.append("--no-sandbox")
    b = p.chromium.launch(executable_path=str(CHROME), env=env, args=args)
    pg = b.new_page(viewport={"width": a.width, "height": a.height}, color_scheme="dark" if a.dark else "light")
    errors = []
    pg.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    pg.goto(a.url, wait_until="networkidle")
    pg.wait_for_timeout(a.wait)
    for sel in a.click:
        pg.click(sel)
        pg.wait_for_timeout(700)
    if a.eval:
        print(pg.evaluate(a.eval))
    sw = pg.evaluate("document.documentElement.scrollWidth")
    pg.screenshot(path=a.out, full_page=a.full)
    print(f"saved {a.out} (scrollWidth={sw}, viewport={a.width})")
    for e in errors[:20]:
        print(e)
    b.close()
