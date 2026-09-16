"""Paraform job catalogue access.

Paraform exposes no public API and www.paraform.com/browse is a login wall
(verified 2026-09: the page returns HTTP 200 with a "Log in | Paraform" form,
docs.paraform.com does not resolve, and the sitemap lists no job URLs).

So this module drives a real browser with a recruiter's own authenticated
session (Playwright + persisted storage state), reads the browse page, and
lets Claude normalise the raw card text into `ParaformJob` records. Results
are cached on disk so a burst of "@claude gold" requests costs one scrape.

Run `python scripts/paraform_login.py` once to create the storage-state file.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path

import anthropic
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout, sync_playwright

from .config import Settings
from .models import ParaformJob, ParaformJobList

log = logging.getLogger(__name__)

LOGIN_MARKERS = ("Log in | Paraform", "Continue with Google", "Continue with Email")
MONEY_PATTERN = re.compile(
    r"(?:\$|€|£|USD|EUR|GBP)\s?\d[\d,.]*\s?[kK]?(?:\s?[-–]\s?(?:\$|€|£)?\s?\d[\d,.]*\s?[kK]?)?",
)
COMP_CLAUSE_PATTERN = re.compile(
    r"[^.\n]*\b(?:salary|salaries|compensation|comp|base pay|pay range|OTE|equity|stock options)\b[^.\n]*[.\n]?",
    re.IGNORECASE,
)

NORMALISE_SYSTEM = """You convert scraped text from a recruiting marketplace into structured job records.
Rules:
- One record per distinct role. Skip navigation text, filters, banners and duplicates.
- job_id: use the URL path after the last '/' when a URL is present; otherwise a short slug of company-title.
- Strip every mention of salary, compensation, equity, OTE or pay ranges from every field. They are irrelevant downstream.
- Infer remote_policy, seniority and role_family from the title and text; use 'unknown' rather than guess wildly.
- Keep description to the substantive requirements, max ~120 words."""


class ParaformAuthError(RuntimeError):
    """The saved session is missing or expired: the browse page rendered the login form."""


class ParaformScrapeError(RuntimeError):
    """We reached the page but could not find any job content on it."""


# --------------------------------------------------------------------------- cache


def _read_cache(path: Path, ttl_minutes: int) -> list[ParaformJob] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    if time.time() - payload.get("fetched_at", 0) > ttl_minutes * 60:
        return None
    return [ParaformJob.model_validate(j) for j in payload.get("jobs", [])]


def _write_cache(path: Path, jobs: list[ParaformJob]) -> None:
    path.write_text(json.dumps({"fetched_at": time.time(), "jobs": [j.model_dump() for j in jobs]}, indent=2))


# --------------------------------------------------------------------------- browser


def _looks_like_login(page: Page) -> bool:
    title = page.title() or ""
    body = page.inner_text("body") if page.locator("body").count() else ""
    return any(m in title for m in LOGIN_MARKERS) or any(m in body for m in LOGIN_MARKERS[1:])


def _scroll_to_bottom(page: Page, max_rounds: int = 25) -> None:
    """Browse pages tend to lazy-load; scroll until the height stops growing."""
    last = -1
    for _ in range(max_rounds):
        height = page.evaluate("document.body.scrollHeight")
        if height == last:
            break
        last = height
        page.mouse.wheel(0, height)
        page.wait_for_timeout(600)


def _harvest_raw_cards(page: Page, browse_url: str) -> list[dict]:
    """Return {text, href} for every anchor that looks like a job card.

    We do not know Paraform's DOM contract and it will change under us, so the
    heuristic is loose: any link on the page whose visible text is long enough
    to be a card (title + company + location) and whose href is on-site.
    """
    origin = re.match(r"https?://[^/]+", browse_url).group(0)
    raw = page.evaluate(
        """() => Array.from(document.querySelectorAll('a[href]')).map(a => ({
              text: (a.innerText || '').trim(),
              href: a.getAttribute('href') || ''
        }))"""
    )
    cards: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        text, href = item["text"], item["href"]
        if len(text) < 25 or href.startswith(("mailto:", "#", "javascript:")):
            continue
        if href.startswith("/"):
            href = origin + href
        if not href.startswith(origin):
            continue
        key = href.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        cards.append({"text": text, "href": key})
    return cards


def _fetch_detail_text(page: Page, url: str) -> str:
    try:
        page.goto(url, wait_until="networkidle", timeout=20_000)
        return page.inner_text("main") if page.locator("main").count() else page.inner_text("body")
    except PlaywrightTimeout:
        log.warning("detail page timed out: %s", url)
        return ""


def scrape_browse_page(settings: Settings) -> list[dict]:
    state = Path(settings.paraform_storage_state)
    if not state.exists():
        raise ParaformAuthError(
            f"No Paraform session at {state}. Run `python scripts/paraform_login.py` first."
        )
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(state))
        page = context.new_page()
        try:
            page.goto(settings.paraform_browse_url, wait_until="networkidle", timeout=45_000)
            if _looks_like_login(page):
                raise ParaformAuthError("Paraform session expired: browse page rendered the login form.")
            _scroll_to_bottom(page)
            cards = _harvest_raw_cards(page, settings.paraform_browse_url)
            if not cards:
                raise ParaformScrapeError("No job cards found on the browse page (DOM may have changed).")
            for card in cards[: settings.paraform_max_detail_pages]:
                card["detail"] = _fetch_detail_text(page, card["href"])
            return cards
        finally:
            context.close()
            browser.close()


# --------------------------------------------------------------------------- normalise


def strip_compensation(text: str) -> str:
    """Drop clauses that talk about pay, then any stray money figures."""
    return MONEY_PATTERN.sub("", COMP_CLAUSE_PATTERN.sub("", text)).strip()


def normalise_cards(cards: list[dict], client: anthropic.Anthropic) -> list[ParaformJob]:
    """Let a cheap model turn raw card text into ParaformJob records.

    Sonnet is deliberate: this is high-volume extraction, not judgment, and
    Haiku 4.5 enforces tighter structured-output schema limits. The matching
    call downstream uses the configured (Opus-class) model.
    """
    lines = []
    for c in cards:
        blob = strip_compensation(c["text"] + ("\n" + c.get("detail", "") if c.get("detail") else ""))
        lines.append(f"URL: {c['href']}\n{blob}\n---")
    response = client.messages.parse(
        model="claude-sonnet-5",
        max_tokens=16000,
        system=NORMALISE_SYSTEM,
        messages=[{"role": "user", "content": "\n".join(lines)}],
        output_format=ParaformJobList,
    )
    jobs = response.parsed_output.jobs
    for job in jobs:  # belt and braces: the model was told, but enforce it
        job.description = strip_compensation(job.description)
        if not job.job_id:
            job.job_id = hashlib.sha1(f"{job.company}|{job.title}".encode()).hexdigest()[:12]
    return jobs


def get_jobs(settings: Settings, client: anthropic.Anthropic, force_refresh: bool = False) -> list[ParaformJob]:
    cache = Path(settings.paraform_cache_path)
    if not force_refresh:
        cached = _read_cache(cache, settings.paraform_cache_ttl_minutes)
        if cached:
            log.info("paraform: %d jobs from cache", len(cached))
            return cached
    cards = scrape_browse_page(settings)
    jobs = normalise_cards(cards, client)
    _write_cache(cache, jobs)
    log.info("paraform: scraped and normalised %d jobs", len(jobs))
    return jobs
