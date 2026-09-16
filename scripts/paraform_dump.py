"""Diagnostic: what does the Paraform browse page contain for this session?

Usage:  python scripts/paraform_dump.py
Prints a short summary you can paste back (no candidate data involved) and
writes paraform_debug.html next to the cache for closer inspection.
"""
from __future__ import annotations

import collections
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.paraform import _harvest_raw_cards, _looks_like_login, _open, _scroll_to_bottom  # noqa: E402

settings = load_settings()
with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context(storage_state=settings.paraform_storage_state)
    page = ctx.new_page()
    _open(page, settings.paraform_browse_url, 45_000)
    print("final url:", page.url)
    print("title:", page.title())
    print("login wall:", _looks_like_login(page))
    _scroll_to_bottom(page)
    hrefs = page.evaluate("() => Array.from(document.querySelectorAll('a[href]')).map(a => a.getAttribute('href'))")
    seg = collections.Counter()
    for h in hrefs:
        m = re.match(r"^(?:https?://www\.paraform\.com)?/([^/?#]+)", h or "")
        seg[m.group(1) if m else "(external/other)"] += 1
    print("anchors:", len(hrefs))
    print("by first path segment:", dict(seg.most_common(15)))
    body = page.inner_text("body")
    print("body chars:", len(body))
    for kw in ("Load more", "Show more", "Next", "roles", "Filters", "Clear"):
        if kw.lower() in body.lower():
            print(f"page text contains: {kw!r}")
    cards = _harvest_raw_cards(page, settings.paraform_browse_url)
    print("harvested cards:", len(cards))
    for c in cards[:5]:
        print("  -", c["href"], "|", c["text"][:80].replace("\n", " / "))
    out = Path(settings.paraform_cache_path).with_name("paraform_debug.html")
    out.write_text(page.content())
    print("saved full page to", out)
    browser.close()
