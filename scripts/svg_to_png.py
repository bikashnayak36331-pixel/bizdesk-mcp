"""Turn docs/demo-session.svg into docs/images/session.png (needs `pip install playwright`)."""

import re
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
svg = ROOT / "docs" / "demo-session.svg"
w, h = (float(x) for x in re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg.read_text()).groups())
with tempfile.TemporaryDirectory() as d:
    page_file = Path(d) / "wrap.html"
    page_file.write_text(f'<body style="margin:0"><img src="{svg.as_uri()}" style="width:{w}px;display:block">')
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": int(w), "height": int(h) + 1}, device_scale_factor=1)
        page.route("http*://**/*", lambda route: route.abort())  # no web fonts: use the local monospace font
        page.goto(page_file.as_uri())
        page.screenshot(path=str(ROOT / "docs" / "images" / "session.png"))
        browser.close()
print("Saved docs/images/session.png")
