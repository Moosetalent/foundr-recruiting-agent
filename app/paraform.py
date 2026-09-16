"""Paraform job catalogue access.

Paraform exposes no public API and www.paraform.com/browse is a login wall.
The browse page itself, however, loads its whole role list with one tRPC
request (`activeRoles.searchActiveRoles`, ~800 roles, a few MB of JSON).
So this module drives a real browser with a recruiter's own authenticated
session (Playwright + persisted storage state), lets the page make that
request, captures the response, and maps the records into `ParaformJob`
deterministically. No LLM is involved in building the catalogue.

Results are cached on disk so a burst of "gold" requests costs one load.
Run `python scripts/paraform_login.py` once to create the storage-state file.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import anthropic
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout, sync_playwright

from .config import Settings
from .models import ParaformJob, ParaformJobList

log = logging.getLogger(__name__)

ROLE_LIST_ENDPOINT = "activeRoles.searchActiveRoles"
MIN_LIST_SIZE = 50  # the first, small searchActiveRoles response is a preview; wait for the full list

LOGIN_MARKERS = ("Log in | Paraform", "Continue with Google", "Continue with Email")
MONEY_PATTERN = re.compile(
    r"(?:\$|€|£|USD|EUR|GBP)\s?\d[\d,.]*\s?[kK]?(?:\s?[-–]\s?(?:\$|€|£)?\s?\d[\d,.]*\s?[kK]?)?",
)
COMP_CLAUSE_PATTERN = re.compile(
    r"[^.\n]*\b(?:salary|salaries|compensation|comp|base pay|pay range|OTE|equity|stock options)\b[^.\n]*[.\n]?",
    re.IGNORECASE,
)


class ParaformAuthError(RuntimeError):
    """The saved session is missing or expired: the browse page rendered the login form."""


class ParaformScrapeError(RuntimeError):
    """We reached the page but could not obtain the role list."""


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

PAGE_READY_JS = (
    "document.querySelectorAll('a[href*=\"/role/\"], a[href*=\"/company/\"]').length >= 10"
    " || document.body.innerText.includes('Continue with Google')"
    " || document.body.innerText.includes('Continue with Email')"
)

SCROLL_JS = """() => {
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
    /load more|show more|see more/i.test((b.innerText || '').trim()) && !b.disabled);
  if (btn) { btn.click(); return true; }
  return false;
}"""


def _looks_like_login(page: Page) -> bool:
    title = page.title() or ""
    body = page.inner_text("body") if page.locator("body").count() else ""
    return any(m in title for m in LOGIN_MARKERS) or any(m in body for m in LOGIN_MARKERS[1:])


def _open(page: Page, url: str, timeout_ms: int) -> None:
    """Load a page without waiting for network idle (Paraform keeps connections open)."""
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        page.wait_for_function(PAGE_READY_JS, timeout=timeout_ms)
    except PlaywrightTimeout:
        log.warning("page did not render a role list within %sms: %s", timeout_ms, url)
    page.wait_for_timeout(1_500)


def _harvest_raw_cards(page: Page, browse_url: str) -> list[dict]:
    """DOM fallback used only when the role-list response could not be captured."""
    origin = re.match(r"https?://[^/]+", browse_url).group(0)
    raw = page.evaluate(
        """() => Array.from(document.querySelectorAll('a[href*="/role/"], a[href*="/company/"]')).map(a => {
              let el = a, hops = 0;
              while (el.parentElement && hops < 4 && (el.innerText || '').trim().length < 80) { el = el.parentElement; hops++; }
              return { text: (el.innerText || '').trim().slice(0, 1500), href: a.getAttribute('href') || '' };
        })"""
    )
    cards: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        href = item["href"]
        if href.startswith("/"):
            href = origin + href
        key = href.split("?")[0].rstrip("/")
        if not href.startswith(origin) or key in seen:
            continue
        seen.add(key)
        cards.append({"text": item["text"], "href": key})
    return cards


def fetch_role_records(settings: Settings, timeout_s: int = 60) -> list[dict]:
    """Open the browse page and capture the full role list the page requests."""
    state = Path(settings.paraform_storage_state)
    if not state.exists():
        raise ParaformAuthError(f"No Paraform session at {state}. Run `python scripts/paraform_login.py` first.")

    captured: list[list[dict]] = []

    def on_response(resp):
        if ROLE_LIST_ENDPOINT not in resp.url or resp.status != 200:
            return
        try:
            payload = resp.json()
        except Exception:
            return
        records = _unwrap_trpc(payload)
        if isinstance(records, list) and len(records) >= MIN_LIST_SIZE:
            captured.append(records)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(state))
        page = context.new_page()
        page.on("response", on_response)
        try:
            try:
                _open(page, settings.paraform_browse_url, 45_000)
            except PlaywrightTimeout as exc:
                raise ParaformScrapeError(f"Paraform browse page did not load within 45s: {exc}") from exc
            if _looks_like_login(page):
                raise ParaformAuthError("Paraform session expired: browse page rendered the login form.")

            deadline = time.time() + timeout_s
            while not captured and time.time() < deadline:
                page.evaluate(SCROLL_JS)
                page.evaluate(LOAD_MORE_JS)
                page.wait_for_timeout(1_500)
            if not captured:
                raise ParaformScrapeError(
                    f"Paraform page loaded but no {ROLE_LIST_ENDPOINT} response with >= {MIN_LIST_SIZE} roles "
                    f"arrived within {timeout_s}s."
                )
            records = max(captured, key=len)
            log.info("paraform: captured %d role records from %s", len(records), ROLE_LIST_ENDPOINT)
            return records
        finally:
            context.close()
            browser.close()


def _unwrap_trpc(payload: Any) -> Any:
    """tRPC + superjson envelope: {"result": {"data": {"json": ...}}} (or a batch list of them)."""
    if isinstance(payload, list) and payload and isinstance(payload[0], dict) and "result" in payload[0]:
        payload = payload[0]
    if isinstance(payload, dict):
        data = payload.get("result", {}).get("data", payload)
        if isinstance(data, dict) and "json" in data:
            return data["json"]
        return data
    return payload


# --------------------------------------------------------------------------- mapping


def strip_compensation(text: str) -> str:
    """Drop clauses that talk about pay, then any stray money figures."""
    return MONEY_PATTERN.sub("", COMP_CLAUSE_PATTERN.sub("", text)).strip()


def _walk(obj: Any, path: str = ""):
    """Yield (path, value) for every leaf in a nested structure."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, obj


def _strings(obj: Any, prefer: tuple[str, ...] = ("name", "label", "title", "value", "city", "state", "country")) -> list[str]:
    """Collect the human-readable strings from a value that may be a string, a list, or nested dicts."""
    if obj is None:
        return []
    if isinstance(obj, str):
        return [obj.strip()] if obj.strip() else []
    if isinstance(obj, (int, float, bool)):
        return []
    if isinstance(obj, list):
        out: list[str] = []
        for item in obj:
            out.extend(_strings(item, prefer))
        return out
    if isinstance(obj, dict):
        for key in prefer:
            if isinstance(obj.get(key), str) and obj[key].strip():
                return [obj[key].strip()]
        out = []
        for v in obj.values():
            if isinstance(v, (str, list, dict)):
                out.extend(_strings(v, prefer))
        return out
    return []


def _first_string(role: dict, key_pattern: str, min_len: int = 0) -> str:
    pat = re.compile(key_pattern, re.IGNORECASE)
    best = ""
    for path, value in _walk(role):
        leaf = path.rsplit(".", 1)[-1]
        if isinstance(value, str) and pat.search(leaf) and len(value) >= min_len and len(value) > len(best):
            best = value
    return best


def _first_number(role: dict, key_pattern: str) -> float:
    pat = re.compile(key_pattern, re.IGNORECASE)
    for path, value in _walk(role):
        leaf = path.rsplit(".", 1)[-1]
        if isinstance(value, (int, float)) and not isinstance(value, bool) and pat.search(leaf):
            return float(value)
    return 0.0


def _seniority_from_title(title: str) -> str:
    t = title.lower()
    if re.search(r"\b(cto|vp|vice president|head of|chief)\b", t):
        return "exec"
    if re.search(r"\b(staff|principal|distinguished)\b", t):
        return "staff"
    if re.search(r"\b(lead|manager|director)\b", t):
        return "lead"
    if re.search(r"\b(senior|sr\.?)\b", t):
        return "senior"
    if re.search(r"\b(junior|jr\.?|intern|entry|associate|new grad)\b", t):
        return "junior"
    return "unknown"


def _remote_policy(role: dict, blob: str) -> str:
    explicit = _first_string(role, r"remote|work_?(arrangement|mode|type)|location_?type|onsite|workplace").lower()
    text = explicit or blob.lower()
    if re.search(r"\bhybrid\b", text):
        return "hybrid"
    if re.search(r"\b(remote|distributed|anywhere)\b", text):
        return "remote"
    if re.search(r"\b(on-?site|in-?office|in person)\b", text):
        return "onsite"
    return "unknown"


def role_to_job(role: dict) -> ParaformJob:
    role_id = str(role.get("id") or hashlib.sha1(json.dumps(role, sort_keys=True, default=str).encode()).hexdigest()[:12])
    title = str(role.get("name") or role.get("title") or "").strip()
    company = " / ".join(dict.fromkeys(_strings(role.get("company"))[:1])) or str(role.get("companyName") or "")
    locations = _strings(role.get("locations") or role.get("location"))
    role_types = _strings(role.get("role_types"))
    tech = _strings(role.get("tech_stack"))
    tags = _strings(role.get("practice_area_tags"))
    description = _first_string(role, r"description|summary|requirements|about|details|overview|pitch|notes", min_len=80)
    blob = " ".join([title, *locations, *role_types, description[:400]])
    job = ParaformJob(
        job_id=role_id,
        title=title or "(untitled role)",
        company=company or "(unknown company)",
        url=f"https://www.paraform.com/role/{role_id}",
        location=", ".join(dict.fromkeys(locations))[:200],
        remote_policy=_remote_policy(role, blob),  # type: ignore[arg-type]
        seniority=_seniority_from_title(title),  # type: ignore[arg-type]
        role_family=", ".join(dict.fromkeys(role_types))[:120].lower(),
        required_skills=list(dict.fromkeys(tech))[:25],
        nice_to_have_skills=list(dict.fromkeys(tags))[:15],
        years_experience_min=_first_number(role, r"^(yoe|years?_?(of_)?exp\w*|min_?yoe|yoe_?min)$"),
        description=strip_compensation(description)[:1500],
        company_stage=_first_string(role, r"stage|funding_?round|series")[:60],
    )
    return job


def _sample_for_debug(role: dict) -> dict:
    """The first record with strings truncated, so the mapping can be checked without a 4 MB dump."""

    def trunc(v):
        if isinstance(v, str):
            return v[:80]
        if isinstance(v, dict):
            return {k: trunc(x) for k, x in list(v.items())[:40]}
        if isinstance(v, list):
            return [trunc(x) for x in v[:3]]
        return v

    return trunc(role)


def records_to_jobs(records: list[dict], cache_path: Path) -> list[ParaformJob]:
    jobs = [role_to_job(r) for r in records if isinstance(r, dict)]
    if records:
        sample = cache_path.with_name("paraform_role_sample.json")
        sample.write_text(json.dumps({"raw": _sample_for_debug(records[0]), "mapped": jobs[0].model_dump()}, indent=1))
    with_loc = sum(1 for j in jobs if j.location)
    with_desc = sum(1 for j in jobs if j.description)
    with_skills = sum(1 for j in jobs if j.required_skills)
    log.info("paraform: mapped %d jobs (%d with location, %d with description, %d with skills)",
             len(jobs), with_loc, with_desc, with_skills)
    return jobs


# --------------------------------------------------------------------------- fallback normalisation (DOM path only)

NORMALISE_SYSTEM = """You convert scraped text from a recruiting marketplace into structured job records.
Rules:
- One record per distinct role. Skip navigation text, filters, banners and duplicates.
- job_id: use the URL path after the last '/' when a URL is present; otherwise a short slug of company-title.
- Strip every mention of salary, compensation, equity, OTE or pay ranges from every field.
- Infer remote_policy, seniority and role_family from the title and text; use 'unknown' rather than guess wildly."""


def normalise_cards(cards: list[dict], client: anthropic.Anthropic) -> list[ParaformJob]:
    lines = [f"URL: {c['href']}\n{strip_compensation(c['text'])}\n---" for c in cards]
    response = client.messages.parse(
        model="claude-sonnet-5",
        max_tokens=16000,
        system=NORMALISE_SYSTEM,
        messages=[{"role": "user", "content": "\n".join(lines)}],
        output_format=ParaformJobList,
    )
    return response.parsed_output.jobs


# --------------------------------------------------------------------------- entrypoint


def get_jobs(settings: Settings, client: anthropic.Anthropic, force_refresh: bool = False) -> list[ParaformJob]:
    cache = Path(settings.paraform_cache_path)
    if not force_refresh:
        cached = _read_cache(cache, settings.paraform_cache_ttl_minutes)
        if cached:
            log.info("paraform: %d jobs from cache", len(cached))
            return cached
    records = fetch_role_records(settings)
    jobs = records_to_jobs(records, cache)
    _write_cache(cache, jobs)
    return jobs
