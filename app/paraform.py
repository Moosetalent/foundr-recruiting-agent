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


SCROLL_JS = """() => {
  // Scroll the window and every scrollable container (the job list is often
  // an inner overflow:auto panel, not the document). Return total link count.
  window.scrollTo(0, document.body.scrollHeight);
  for (const el of document.querySelectorAll('*')) {
    const st = getComputedStyle(el);
    if ((st.overflowY === 'auto' || st.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 50) {
      el.scrollTop = el.scrollHeight;
    }
  }
  return document.querySelectorAll('a[href]').length;
}"""

LOAD_MORE_JS = """() => {
  const btn = Array.from(document.querySelectorAll('button, a')).find(b =>
    /load more|show more|see more|next/i.test((b.innerText || '').trim()) && !b.disabled);
  if (btn) { btn.click(); return true; }
  return false;
}"""


def _scroll_to_bottom(page: Page, max_rounds: int = 40) -> None:
    """Browse pages lazy-load; scroll window and inner panels until the link
    count stops growing, clicking any 'load more' style button on the way."""
    stable = 0
    last = -1
    for _ in range(max_rounds):
        count = page.evaluate(SCROLL_JS)
        clicked = page.evaluate(LOAD_MORE_JS)
        page.wait_for_timeout(900 if clicked else 600)
        if count == last:
            stable += 1
            if stable >= 3:
                break
        else:
            stable = 0
        last = count


def _harvest_raw_cards(page: Page, browse_url: str) -> list[dict]:
    """Return {text, href} for every anchor that looks like a job card.

    We do not know Paraform's DOM contract and it will change under us, so the
    heuristic is loose: any link on the page whose visible text is long enough
    to be a card (title + company + location) and whose href is on-site.
    """
    origin = re.match(r"https?://[^/]+", browse_url).group(0)
    raw = page.evaluate(
        """() => Array.from(document.querySelectorAll('a[href]')).map(a => {
              // the card is usually the closest sizeable ancestor, not the anchor itself
              let el = a, hops = 0;
              while (el.parentElement && hops < 4 && (el.innerText || '').trim().length < 80) { el = el.parentElement; hops++; }
              return { text: (el.innerText || '').trim().slice(0, 1500),
                       anchor_text: (a.innerText || '').trim().slice(0, 200),
                       href: a.getAttribute('href') || '' };
        })"""
    )
    job_like = re.compile(r"/(role|roles|job|jobs|company|companies)/", re.IGNORECASE)
    cards: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        text, href = item["text"], item["href"]
        if href.startswith(("mailto:", "#", "javascript:")):
            continue
        if href.startswith("/"):
            href = origin + href
        if not href.startswith(origin):
            continue
        key = href.split("?")[0].rstrip("/")
        is_job = bool(job_like.search(key))
        if not is_job and len(text) < 25:
            continue
        if not is_job and len(text) > 600:  # nav/footer blobs, not a card
            continue
        if key in seen or key == browse_url.rstrip("/"):
            continue
        seen.add(key)
        cards.append({"text": text, "href": key, "job_link": is_job})
    job_cards = [c for c in cards if c["job_link"]]
    log.info("paraform: %d anchors on page, %d unique on-site, %d look like role/company links",
             len(raw), len(cards), len(job_cards))
    return job_cards or cards


PAGE_READY_JS = (
    "document.querySelectorAll('a[href]').length > 10"
    " || document.body.innerText.includes('Continue with Google')"
    " || document.body.innerText.includes('Continue with Email')"
)


def _open(page: Page, url: str, timeout_ms: int) -> None:
    """Load a page without waiting for network idle.

    Paraform keeps long-lived connections open (live updates, analytics), so
    'networkidle' never fires. Wait for the document, then for either the
    login form or a rendered list of links, then a short settle.
    """
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        page.wait_for_function(PAGE_READY_JS, timeout=timeout_ms)
    except PlaywrightTimeout:
        log.warning("page did not render links within %sms: %s", timeout_ms, url)
    page.wait_for_timeout(2_000)


def _fetch_detail_text(page: Page, url: str) -> str:
    try:
        _open(page, url, 20_000)
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
            try:
                _open(page, settings.paraform_browse_url, 45_000)
            except PlaywrightTimeout as exc:
                raise ParaformScrapeError(f"Paraform browse page did not load within 45s: {exc}") from exc
            if _looks_like_login(page):
                raise ParaformAuthError("Paraform session expired: browse page rendered the login form.")
            _scroll_to_bottom(page)
            cards = _harvest_raw_cards(page, settings.paraform_browse_url)
            debug = Path(settings.paraform_cache_path).with_name("paraform_cards_debug.json")
            debug.write_text(json.dumps({"url": page.url, "title": page.title(), "cards": cards}, indent=1))
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
