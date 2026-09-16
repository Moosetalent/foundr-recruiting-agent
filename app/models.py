"""Pydantic schemas shared by the scraper, the matcher and the Slack renderer.

These double as the structured-output schemas handed to Claude, so field
descriptions are written for the model as much as for humans.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ParaformJob(BaseModel):
    """One role from the Paraform browse page. Compensation is deliberately absent."""

    job_id: str = Field(description="Stable id: the Paraform job URL slug or a hash of title+company.")
    title: str
    company: str
    url: str = Field(description="Absolute Paraform URL for the role.")
    location: str = Field(default="", description="City/region as shown, e.g. 'San Francisco, CA'.")
    remote_policy: Literal["remote", "hybrid", "onsite", "unknown"] = "unknown"
    seniority: Literal["junior", "mid", "senior", "staff", "lead", "exec", "unknown"] = "unknown"
    role_family: str = Field(default="", description="Normalised family, e.g. 'backend engineer', 'account executive'.")
    required_skills: list[str] = Field(default_factory=list)
    nice_to_have_skills: list[str] = Field(default_factory=list)
    years_experience_min: float | None = None
    description: str = Field(default="", description="Card or detail text with compensation stripped out.")
    company_stage: str = Field(default="", description="e.g. 'Seed', 'Series B', if visible.")


class ParaformJobList(BaseModel):
    jobs: list[ParaformJob]


class CandidateProfile(BaseModel):
    """What the matcher needs to know about the candidate. Nothing about pay."""

    name: str | None = None
    current_title: str | None = None
    current_company: str | None = None
    location: str | None = Field(default=None, description="City/region/country the candidate is based in.")
    remote_preference: Literal["remote", "hybrid", "onsite", "flexible", "unknown"] = "unknown"
    open_to_relocation: bool | None = None
    years_experience: float | None = Field(default=None, description="Total relevant professional experience in years.")
    seniority: Literal["junior", "mid", "senior", "staff", "lead", "exec", "unknown"] = "unknown"
    role_families: list[str] = Field(default_factory=list, description="Ordered by fit, e.g. ['backend engineer', 'full stack engineer'].")
    core_skills: list[str] = Field(default_factory=list, description="Skills with real depth (years or shipped work).")
    secondary_skills: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    work_authorization_notes: str | None = Field(default=None, description="Only if explicitly stated in the source material.")
    summary: str = Field(description="Three sentences a recruiter would say out loud about this person.")
    source_gaps: list[str] = Field(default_factory=list, description="Things the profile did not tell us that would change the match, e.g. 'no location stated'.")


class JobMatch(BaseModel):
    job_id: str = Field(description="Must be one of the job_id values from the catalogue.")
    job_title: str
    company: str
    job_url: str
    score: int = Field(ge=0, le=100, description="0-100 fit on skills, seniority, location and domain. Compensation is never a factor.")
    why_fit: list[str] = Field(description="2-4 concrete overlaps, each citing evidence from the profile.")
    flags: list[str] = Field(description="0-3 concrete gaps or risks. Empty list if none.")


class MatchReport(BaseModel):
    candidate_name: str
    matches: list[JobMatch] = Field(description="Best matches first. Return fewer than requested rather than pad with weak fits.")
    no_match_reason: str | None = Field(default=None, description="Set only when matches is empty: why nothing on the board fits.")
