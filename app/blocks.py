"""Block Kit rendering for the match notification. Pure function: no I/O.

Layout is deliberately compact: one header, one line of candidate context,
then per role a title line with the score and a small grey context line
carrying the fit and the flag. Five roles fit on one screen.
"""
from __future__ import annotations

from .models import CandidateProfile, MatchReport

SUBMIT_ACTION_ID = "submit_to_paraform"


def score_emoji(score: int) -> str:
    if score >= 85:
        return "🟢"
    if score >= 70:
        return "🟡"
    return "🔴"


def _candidate_line(profile: CandidateProfile, name: str) -> str:
    parts = [f"*{name or 'Unknown candidate'}*"]
    role = profile.current_title
    if role and profile.current_company:
        role += f" @ {profile.current_company}"
    if role:
        parts.append(role)
    if profile.years_experience > 0:
        parts.append(f"{profile.years_experience:g} yrs")
    loc = profile.location or "location not stated"
    if profile.remote_preference not in ("unknown", ""):
        loc += f", {profile.remote_preference}"
    parts.append(loc)
    return " · ".join(parts)


def _short(items: list[str], limit: int) -> str:
    return " · ".join(i.strip().rstrip(".") for i in items[:limit] if i.strip())


def build_match_blocks(profile: CandidateProfile, report: MatchReport, paraform_browse_url: str) -> list[dict]:
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": "🎯 Top Paraform Matches", "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": _candidate_line(profile, report.candidate_name or profile.name)}},
        {"type": "divider"},
    ]

    if not report.matches:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": f"*No strong match on the board right now.* {report.no_match_reason}".strip()}})
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"<{paraform_browse_url}|Open Paraform browse>"}]})
        return blocks

    for i, m in enumerate(report.matches, start=1):
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": f"{score_emoji(m.score)} *{m.score}%*  *{i}. <{m.job_url}|{m.job_title}>* — {m.company}"}})
        line = f"✅ {_short(m.why_fit, 2)}" if m.why_fit else "✅ fit not captured"
        if m.flags:
            line += f"\n⚠️ {_short(m.flags, 1)}"
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": line[:2900]}]})

    top = report.matches[0]
    blocks.append({"type": "actions", "elements": [{
        "type": "button", "style": "primary", "action_id": SUBMIT_ACTION_ID,
        "text": {"type": "plain_text", "text": "Submit Candidate to Paraform", "emoji": True},
        "url": top.job_url, "value": top.job_id,
    }]})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                   "text": f"{len(report.matches)} of {report.considered} roles considered · scored on skills, seniority, location, domain · compensation ignored"}]})
    return blocks


def fallback_text(report: MatchReport) -> str:
    if not report.matches:
        return f"No strong Paraform match for {report.candidate_name}."
    top = report.matches[0]
    return f"Top Paraform match for {report.candidate_name}: {top.job_title} at {top.company} ({top.score}%)."
