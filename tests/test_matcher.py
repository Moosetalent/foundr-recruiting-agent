from app.matcher import prefilter_jobs
from app.models import CandidateProfile, ParaformJob
from app.paraform import strip_compensation
from app.slack_io import is_gold_request


def test_gold_trigger_is_case_insensitive_and_word_bound():
    assert is_gold_request("<@U123> Gold")
    assert is_gold_request("<@U123> gold please")
    assert not is_gold_request("<@U123> goldilocks")
    assert not is_gold_request("<@U123> silver")


def test_prefilter_prefers_role_family_and_location():
    profile = CandidateProfile(location="London", role_families=["backend engineer"], core_skills=["Python"], summary="x")
    jobs = [
        ParaformJob(job_id="1", title="Backend Engineer", company="A", url="u", location="London", remote_policy="hybrid", required_skills=["Python"]),
        ParaformJob(job_id="2", title="Account Executive", company="B", url="u", location="New York", remote_policy="onsite"),
        ParaformJob(job_id="3", title="Backend Engineer", company="C", url="u", location="Austin", remote_policy="remote"),
    ]
    kept = prefilter_jobs(profile, jobs, cap=2)
    assert [j.job_id for j in kept] == ["1", "3"]


def test_strip_compensation():
    out = strip_compensation("Senior Engineer. $180k - $220k base plus equity. Remote.")
    assert "$" not in out and "180" not in out and "Remote" in out
