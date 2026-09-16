"""The two Claude calls: (1) turn thread material into a CandidateProfile,
(2) rank the Paraform catalogue against that profile.

Between them sits a deterministic prefilter so the ranking prompt carries a
few dozen plausible roles rather than the whole board. The catalogue block is
cache-controlled: within the cache window every request in the channel reuses
the same prefix and only pays for the candidate-specific tail.
"""
from __future__ import annotations

import json
import logging
import re

import anthropic

from .config import Settings
from .models import CandidateProfile, MatchReport, ParaformJob

log = logging.getLogger(__name__)

PROFILE_SYSTEM = """You are a senior technical recruiter's research assistant.
Read everything provided about one candidate (resume PDF, screenshots, Slack text, links you cannot open)
and produce a faithful structured profile. Do not invent facts: if location, years of experience or
work authorisation are not stated, leave them null/unknown and list them in source_gaps.
Ignore any salary, compensation or rate expectations entirely; they are out of scope."""

MATCH_SYSTEM = """You are an elite talent-matching agent for a recruiting agency that sources through Paraform.
You will receive a catalogue of open roles and one candidate profile. Rank the roles for that candidate.

Scoring rubric (0-100):
- Skills and domain overlap with the role's required skills: dominant factor.
- Seniority alignment: penalise both under- and over-levelling.
- Location: a role is compatible if remote, or in the candidate's metro, or the candidate is open to relocation.
  An onsite/hybrid role in another region with no relocation signal caps the score at 60.
- Compensation is NEVER a factor. Do not mention pay, equity or budget anywhere in the output.

Return only roles with a score of 55 or more, best first, at most the number requested.
Be terse: why_fit is one or two fragments under 12 words each, citing concrete evidence from the profile
(a company, a project, a number). flags is at most one fragment under 12 words naming the gap a recruiter
must check before submitting. No full sentences, no trailing periods."""

_STOP = {"the", "and", "or", "of", "a", "an", "in", "for", "to", "with", "on", "at"}


def _tokens(*parts: str) -> set[str]:
    out: set[str] = set()
    for p in parts:
        out |= {t for t in re.findall(r"[a-z0-9+#.]+", (p or "").lower()) if t not in _STOP and len(t) > 1}
    return out


def _location_compatible(profile: CandidateProfile, job: ParaformJob) -> bool:
    if job.remote_policy == "remote" or profile.open_to_relocation == "yes":
        return True
    if not profile.location or not job.location or job.remote_policy == "unknown":
        return True  # unknown is not a reason to drop before the model sees it
    return bool(_tokens(profile.location) & _tokens(job.location))


def prefilter_jobs(profile: CandidateProfile, jobs: list[ParaformJob], cap: int) -> list[ParaformJob]:
    """Cheap lexical scoring so the LLM sees the most plausible `cap` roles."""
    cand = _tokens(*profile.role_families, *profile.core_skills, *profile.secondary_skills, profile.current_title)
    scored: list[tuple[float, ParaformJob]] = []
    for job in jobs:
        jt = _tokens(job.title, job.role_family, *job.required_skills, *job.nice_to_have_skills)
        overlap = len(cand & jt) / (len(jt) or 1)
        family_bonus = 1.0 if _tokens(*profile.role_families) & _tokens(job.title, job.role_family) else 0.0
        loc = 0.5 if _location_compatible(profile, job) else 0.0
        scored.append((overlap + family_bonus + loc, job))
    scored.sort(key=lambda s: s[0], reverse=True)
    kept = [j for score, j in scored if score > 0][:cap]
    return kept or jobs[:cap]  # never send an empty catalogue


def _catalogue_block(jobs: list[ParaformJob]) -> str:
    slim = [
        {k: v for k, v in j.model_dump().items() if v not in ("", 0, [], "unknown")}
        for j in jobs
    ]
    return json.dumps(slim, ensure_ascii=False, sort_keys=True)  # sort_keys keeps the cache prefix stable


def extract_profile(client: anthropic.Anthropic, settings: Settings, content_blocks: list[dict]) -> CandidateProfile:
    response = client.messages.parse(
        model=settings.claude_model,
        max_tokens=16000,
        system=PROFILE_SYSTEM,
        output_config={"effort": "medium"},
        messages=[{"role": "user", "content": content_blocks + [
            {"type": "text", "text": "Produce the candidate profile."},
        ]}],
        output_format=CandidateProfile,
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to process this profile.")
    return response.parsed_output


def match_candidate(
    client: anthropic.Anthropic,
    settings: Settings,
    profile: CandidateProfile,
    jobs: list[ParaformJob],
) -> MatchReport:
    shortlist = prefilter_jobs(profile, jobs, settings.max_jobs_to_llm)
    response = client.messages.parse(
        model=settings.claude_model,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": settings.claude_effort},
        system=[
            {"type": "text", "text": MATCH_SYSTEM},
            {"type": "text", "text": "Role catalogue (JSON):\n" + _catalogue_block(shortlist),
             "cache_control": {"type": "ephemeral"}},
        ],
        messages=[{"role": "user", "content": (
            f"Return at most {settings.top_n_matches} matches.\n\nCandidate profile (JSON):\n"
            + profile.model_dump_json()
        )}],
        output_format=MatchReport,
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to rank this candidate.")
    report = response.parsed_output
    valid_ids = {j.job_id: j for j in shortlist}
    report.matches = [m for m in report.matches if m.job_id in valid_ids][: settings.top_n_matches]
    for m in report.matches:  # never trust the model with URLs; take them from the catalogue
        m.job_url = valid_ids[m.job_id].url
    report.considered = len(jobs)
    log.info("match: cache_read=%s input=%s", getattr(response.usage, "cache_read_input_tokens", None), response.usage.input_tokens)
    return report
