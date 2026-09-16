"""Diagnostic: how does the Paraform browse page load its role list?

Usage:  python scripts/paraform_dump.py
Records every JSON response the page fetches while loading, scrolling and
clicking 'Show more', then prints a summary to paste back: endpoint URLs,
response shapes (keys only, no values), link counts. Also writes
paraform_debug.html and paraform_debug_responses.json next to the cache.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.paraform import LOAD_MORE_JS, SCROLL_JS, _harvest_raw_cards, _looks_like_login, _open  # noqa: E402

settings = load_settings()
captured: list[dict] = []


def shape(obj, depth=0):
    """Describe a JSON value by structure only."""
    if depth > 4:
        return "..."
    if isinstance(obj, dict):
        return {k: shape(v, depth + 1) for k, v in list(obj.items())[:25]}
    if isinstance(obj, list):
        return [f"list[{len(obj)}]", shape(obj[0], depth + 1) if obj else None]
    return type(obj).__name__


def on_response(resp):
    ctype = resp.headers.get("content-type", "")
    if "json" not in ctype and "text/plain" not in ctype:
        return
    try:
        body = resp.text()
    except Exception:
        return
    entry = {"url": resp.url, "status": resp.status, "method": resp.request.method, "bytes": len(body)}
    try:
        entry["post_data"] = (resp.request.post_data or "")[:500]
    except Exception:
        pass
    try:
        parsed = json.loads(body)
        entry["shape"] = shape(parsed)
    except Exception:
        entry["shape"] = "not-json: " + body[:120].replace("\n", " ")
    captured.append(entry)


with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context(storage_state=settings.paraform_storage_state)
    page = ctx.new_page()
    page.on("response", on_response)
    _open(page, settings.paraform_browse_url, 45_000)
    print("final url:", page.url)
    print("title:", page.title())
    print("login wall:", _looks_like_login(page))

    def role_links():
        return page.evaluate("() => document.querySelectorAll('a[href*=\"/role/\"], a[href*=\"/company/\"]').length")

    print("role/company links after load:", role_links())
    for i in range(6):
        page.evaluate(SCROLL_JS)
        clicked = page.evaluate(LOAD_MORE_JS)
        page.wait_for_timeout(2500)
        print(f"round {i+1}: clicked show-more={clicked}, role/company links={role_links()}, json responses so far={len(captured)}")

    body = page.inner_text("body")
    print("body chars:", len(body))
    m = re.search(r"(\d[\d,]*)\s+(roles|jobs)", body, re.IGNORECASE)
    print("count text on page:", m.group(0) if m else "none found")
    cards = _harvest_raw_cards(page, settings.paraform_browse_url)
    print("harvested cards:", len(cards))

    print("\n=== JSON responses (url | status | bytes) ===")
    for e in captured:
        print(f"{e['method']} {e['url'][:160]} | {e['status']} | {e['bytes']}")
    big = sorted((e for e in captured if e["bytes"] > 2000), key=lambda e: -e["bytes"])[:4]
    print("\n=== shapes of the largest responses ===")
    for e in big:
        print(e["url"][:160])
        if e.get("post_data"):
            print("  post:", e["post_data"][:300])
        print("  ", json.dumps(e["shape"])[:1500])

    out_dir = Path(settings.paraform_cache_path).parent
    (out_dir / "paraform_debug.html").write_text(page.content())
    (out_dir / "paraform_debug_responses.json").write_text(json.dumps(captured, indent=1))
    print("\nsaved paraform_debug.html and paraform_debug_responses.json")
    browser.close()
