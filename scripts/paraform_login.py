"""One-time interactive login to Paraform. Saves cookies/localStorage to the
path in PARAFORM_STORAGE_STATE so the headless scraper can reuse the session.

Usage:  python scripts/paraform_login.py
Then log in with your recruiter account in the window that opens. When you
can see the browse page, come back to the terminal and press Enter.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()
state_path = Path(os.environ.get("PARAFORM_STORAGE_STATE", "./paraform_state.json"))
browse_url = os.environ.get("PARAFORM_BROWSE_URL", "https://www.paraform.com/browse")

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto(browse_url)
    print(f"Log in to Paraform in the browser window, land on {browse_url}, then press Enter here.")
    sys.stdin.readline()
    context.storage_state(path=str(state_path))
    print(f"Saved session to {state_path}")
    browser.close()
