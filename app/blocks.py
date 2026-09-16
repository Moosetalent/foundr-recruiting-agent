"""Block Kit rendering for the match notification. Pure function: no I/O."""
from __future__ import annotations

from .models import CandidateProfile, MatchReport

SUBMIT_ACTION_ID = "submit_to_paraform"


def score_emoji(score: int) -> str:
    if score >= 85:
        return "🟢"
    if score >= 70:
        return "🟡"
    return "🔴"


def _bullets(items: list[str], empty: str) -> str:
    return "\n".join(f"• {i}" for i in items) if items else f"• {empty}"


def _experience_line(profile: CandidateProfile) -> str:
    yrs = f"{profile.years_experience:g} yrs" if profile.years_experience > 0 else "n/a"
    sen = profile.seniority if profile.seniority != "unknown" else ""
    return " · ".join(p for p in (yrs, sen) if p)


def build_match_blocks(profile: CandidateProfile, report: MatchReport, paraform_browse_url: str) -> list[dict]:
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": "🎯 Top Paraform Match Found", "emoji": True}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Name*\n{report.candidate_name or profile.name or 'Unknown'}"},
            {"type": "mrkdwn", "text": f"*Current Role*\n{profile.current_title or 'n/a'}"
                                        + (f" @ {profile.current_company}" if profile.current_company else "")},
            {"type": "mrkdwn", "text": f"*Experience*\n{_experience_line(profile)}"},
            {"type": "mrkdwn", "text": f"*Location*\n{profile.location or 'not stated'}"
                                        + (f" ({profile.remote_preference})" if profile.remote_preference != 'unknown' else "")},
        ]},
        {"type": "divider"},
    ]

    if not report.matches:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": f"*No strong match on the board right now.*\n{report.no_match_reason or ''}".strip()}})
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"<{paraform_browse_url}|Open Paraform browse>"}]})
        return blocks

    for i, m in enumerate(report.matches, start=1):
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": f"*{i}. <{m.job_url}|{m.job_title}>* — {m.company}\n{score_emoji(m.score)} *{m.score}% Match*"}})
        blocks.append({"type": "section", "fields": [
            {"type": "mrkdwn", "text": "*Why it's a fit*\n" + _bullets(m.why_fit, "no evidence captured")},
            {"type": "mrkdwn", "text": "*Potential flags*\n" + _bullets(m.flags, "none identified")},
        ]})
        if i < len(report.matches):
            blocks.append({"type": "divider"})

    top = report.matches[0]
    blocks.append({"type": "divider"})
    blocks.append({"type": "actions", "elements": [{
        "type": "button", "style": "primary", "action_id": SUBMIT_ACTION_ID,
        "text": {"type": "plain_text", "text": "Submit Candidate to Paraform", "emoji": True},
        "url": top.job_url, "value": top.job_id,
    }]})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                   "text": "Scored on skills, seniority, location and domain. Compensation ignored by design."}]})
    return blocks


def fallback_text(report: MatchReport) -> str:
    if not report.matches:
        return f"No strong Paraform match for {report.candidate_name}."
    top = report.matches[0]
    return f"Top Paraform match for {report.candidate_name}: {top.job_title} at {top.company} ({top.score}%)."
