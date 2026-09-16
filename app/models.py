"""Pydantic schemas shared by the scraper, the matcher and the Slack renderer.

These double as the structured-output schemas handed to Claude, so field
descriptions are written for the model as much as for humans.

Two rules keep the API's structured-output compiler happy:
- no `X | None` unions (each becomes an anyOf); unknowns are "", 0 or "unknown"
- every property is marked required in the JSON schema. The API allows at most
  12 optional properties and rejects the schema as "too complex" beyond that,
  and Pydantic marks any field with a default as optional. `StrictOut` below
  forces `required` to cover every property while keeping Python defaults.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Seniority = Literal["junior", "mid", "senior", "staff", "lead", "exec", "unknown"]


def _require_everything(schema: dict[str, Any], model: type[BaseModel]) -> None:
    schema["required"] = list(schema.get("properties", {}))
    schema["additionalProperties"] = False


class StrictOut(BaseModel):
    """Base for models returned by Claude: all properties required in the schema."""

    model_config = ConfigDict(json_schema_extra=_require_everything)


class ParaformJob(StrictOut):
    """One role from the Paraform browse page. Compensation is deliberately absent."""

    job_id: str = Field(description="Stable id: the Paraform job URL slug or a hash of title+company.")
    title: str
    company: str
    url: str = Field(description="Absolute Paraform URL for the role.")
    location: str = Field(default="", description="City/region as shown, e.g. 'San Francisco, CA'. Empty if not shown.")
    remote_policy: Literal["remote", "hybrid", "onsite", "unknown"] = "unknown"
    seniority: Seniority = "unknown"
    role_family: str = Field(default="", description="Normalised family, e.g. 'backend engineer', 'account executive'.")
    required_skills: list[str] = Field(default_factory=list)
    nice_to_have_skills: list[str] = Field(default_factory=list)
    years_experience_min: float = Field(default=0, description="Minimum years asked for; 0 if not stated.")
    description: str = Field(default="", description="Card or detail text with compensation stripped out.")
    company_stage: str = Field(default="", description="e.g. 'Seed', 'Series B', if visible. Empty otherwise.")


class ParaformJobList(StrictOut):
    jobs: list[ParaformJob]


class CandidateProfile(StrictOut):
    """What the matcher needs to know about the candidate. Nothing about pay."""

    name: str = Field(default="", description="Full name, or empty if not stated.")
    current_title: str = Field(default="", description="Empty if not stated.")
    current_company: str = Field(default="", description="Empty if not stated.")
    location: str = Field(default="", description="City/region/country the candidate is based in. Empty if not stated.")
    remote_preference: Literal["remote", "hybrid", "onsite", "flexible", "unknown"] = "unknown"
    open_to_relocation: Literal["yes", "no", "unknown"] = "unknown"
    years_experience: float = Field(default=0, description="Total relevant professional experience in years. 0 if not stated.")
    seniority: Seniority = "unknown"
    role_families: list[str] = Field(default_factory=list, description="Ordered by fit, e.g. ['backend engineer', 'full stack engineer'].")
    core_skills: list[str] = Field(default_factory=list, description="Skills with real depth (years or shipped work).")
    secondary_skills: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    work_authorization_notes: str = Field(default="", description="Only if explicitly stated in the source material. Empty otherwise.")
    summary: str = Field(description="Three sentences a recruiter would say out loud about this person.")
    source_gaps: list[str] = Field(default_factory=list, description="Things the profile did not tell us that would change the match, e.g. 'no location stated'.")


class JobMatch(StrictOut):
    job_id: str = Field(description="Must be one of the job_id values from the catalogue.")
    job_title: str
    company: str
    job_url: str
    score: int = Field(description="0-100 fit on skills, seniority, location and domain. Compensation is never a factor.")
    why_fit: list[str] = Field(description="2-4 concrete overlaps, each citing evidence from the profile.")
    flags: list[str] = Field(description="0-3 concrete gaps or risks. Empty list if none.")


class MatchReport(StrictOut):
    candidate_name: str
    matches: list[JobMatch] = Field(description="Best matches first. Return fewer than requested rather than pad with weak fits.")
    no_match_reason: str = Field(default="", description="Only when matches is empty: why nothing on the board fits. Empty otherwise.")
