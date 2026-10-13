"""Rule metadata, transformation results and migration plans."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from codeshift.models.findings import Finding, Severity, TransformKind


class Risk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RuleSpec(BaseModel):
    """Metadata describing one migration rule. Behaviour lives in the rule class."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str
    pack: str
    language: str = "python"
    severity: Severity = Severity.MEDIUM
    risk: Risk = Risk.MEDIUM
    transform_kind: TransformKind = TransformKind.DETERMINISTIC
    safe_for_auto_apply: bool = False
    requires_review: bool = True
    # Rule only applies when the project's target Python is at least this version.
    min_target_python: str | None = None
    # Version in which the source API was deprecated / removed, for reporting.
    deprecated_in: str | None = None
    removed_in: str | None = None
    semantic_notes: str = ""
    verification: list[str] = Field(default_factory=lambda: ["syntax", "structural"])
    references: list[str] = Field(default_factory=list)


class TransformStatus(StrEnum):
    UNCHANGED = "unchanged"
    PROPOSED = "proposed"
    CONFLICT = "conflict"
    FAILED = "failed"


class ChangeLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    start_line: int
    end_line: int
    symbol: str | None = None


class TransformationResult(BaseModel):
    """Outcome of transforming one file in memory. Nothing is written to disk."""

    model_config = ConfigDict(extra="forbid")

    file: str
    original_hash: str
    proposed_hash: str
    modified_source: str
    applied_rules: list[str] = Field(default_factory=list)
    locations: list[ChangeLocation] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    verification_requirements: list[str] = Field(default_factory=list)
    status: TransformStatus = TransformStatus.UNCHANGED
    requires_review: bool = False
    origin: str = "deterministic"


class ChangeStatus(StrEnum):
    PROPOSED = "proposed"
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"


class FileChange(BaseModel):
    """A proposed (and later applied) modification of one file, as persisted."""

    model_config = ConfigDict(extra="forbid")

    file: str
    original_hash: str
    proposed_hash: str
    rule_ids: list[str]
    locations: list[ChangeLocation] = Field(default_factory=list)
    origin: str = "deterministic"
    requires_review: bool = False
    warnings: list[str] = Field(default_factory=list)
    diff: str = ""
    status: ChangeStatus = ChangeStatus.PROPOSED


class Proposal(BaseModel):
    """Something CodeShift found but will not change by itself."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    functions: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    call_sites: list[str] = Field(default_factory=list)


class ConversionGroup(BaseModel):
    """A set of functions that must become async together or not at all."""

    model_config = ConfigDict(extra="forbid")

    id: str
    seeds: list[str]
    callers: list[str] = Field(default_factory=list)
    converted: bool = False
    used_llm: bool = False
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    call_sites: list[str] = Field(default_factory=list)


class LLMUsage(BaseModel):
    """Provider metadata recorded for a run. Never contains credentials."""

    model_config = ConfigDict(extra="forbid")

    provider: str = "none"
    model: str = ""
    requests: int = 0
    failed_requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    redactions: int = 0


class RunStatus(StrEnum):
    PLANNED = "planned"
    APPLIED = "applied"
    VERIFIED = "verified"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class Timings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scan_s: float = 0.0
    analysis_s: float = 0.0
    transform_s: float = 0.0
    verification_s: float = 0.0
    total_s: float = 0.0


class MigrationPlan(BaseModel):
    """Everything a `plan` run decided, ready to be reviewed and applied."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    repo: str
    created_at: str
    rules: list[str]
    files_analyzed: int = 0
    parse_failures: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    changes: list[FileChange] = Field(default_factory=list)
    groups: list[ConversionGroup] = Field(default_factory=list)
    proposals: list[Proposal] = Field(default_factory=list)
    converted_functions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    llm: LLMUsage = Field(default_factory=LLMUsage)
    timings: Timings = Field(default_factory=Timings)
