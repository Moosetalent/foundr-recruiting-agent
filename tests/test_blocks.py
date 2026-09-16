from app.blocks import build_match_blocks, fallback_text, score_emoji
from app.models import CandidateProfile, JobMatch, MatchReport


def _sample():
    profile = CandidateProfile(
        name="Priya Natarajan", current_title="Senior Backend Engineer", current_company="Stripe",
        location="San Francisco, CA", remote_preference="hybrid", years_experience=8, seniority="senior",
        role_families=["backend engineer"], core_skills=["Go", "Kubernetes", "PostgreSQL"], summary="x",
    )
    report = MatchReport(candidate_name="Priya Natarajan", matches=[
        JobMatch(job_id="a", job_title="Staff Backend Engineer", company="Decagon", job_url="https://www.paraform.com/x/a",
                 score=94, why_fit=["8 yrs Go at Stripe"], flags=[]),
        JobMatch(job_id="b", job_title="Senior Platform Engineer", company="Hightouch", job_url="https://www.paraform.com/x/b",
                 score=78, why_fit=["Kubernetes"], flags=["Role is onsite in NYC"]),
    ])
    return profile, report


def test_score_emoji_bands():
    assert score_emoji(94) == "🟢" and score_emoji(78) == "🟡" and score_emoji(50) == "🔴"


def test_blocks_shape():
    profile, report = _sample()
    blocks = build_match_blocks(profile, report, "https://www.paraform.com/browse")
    assert blocks[0]["type"] == "header"
    assert blocks[0]["text"]["text"] == "🎯 Top Paraform Match Found"
    assert blocks[-2]["type"] == "actions"
    assert blocks[-2]["elements"][0]["text"]["text"] == "Submit Candidate to Paraform"
    assert blocks[-2]["elements"][0]["url"] == "https://www.paraform.com/x/a"
    assert sum(1 for b in blocks if b["type"] == "divider") >= 2
    assert "94% Match" in fallback_text(report) or "94%" in fallback_text(report)


def test_no_match_renders_reason():
    profile, _ = _sample()
    report = MatchReport(candidate_name="Priya", matches=[], no_match_reason="Board is GTM-heavy this week.")
    blocks = build_match_blocks(profile, report, "https://www.paraform.com/browse")
    assert any("GTM-heavy" in b.get("text", {}).get("text", "") for b in blocks if b["type"] == "section")
