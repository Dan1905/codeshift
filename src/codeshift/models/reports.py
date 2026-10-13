"""Persisted run record and the report built from it."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from codeshift.models.migrations import MigrationPlan, RunStatus
from codeshift.models.verification import VerificationResult


class RunRecord(BaseModel):
    """One migration run as stored in SQLite."""

    model_config = ConfigDict(extra="forbid")

    plan: MigrationPlan
    status: RunStatus = RunStatus.PLANNED
    updated_at: str
    approved_by_user: bool = False
    verification: VerificationResult | None = None
    errors: list[str] = Field(default_factory=list)
    rollback_status: str = "not_requested"

    @property
    def run_id(self) -> str:
        return self.plan.run_id


class Report(BaseModel):
    """Machine-readable migration report."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    repo: str
    status: str
    created_at: str
    updated_at: str
    rules: list[str]
    files_analyzed: int
    files_modified: int
    transformations: int
    findings_by_rule: dict[str, int]
    changes: list[dict[str, object]]
    groups: list[dict[str, object]]
    proposals: list[dict[str, object]]
    verification: dict[str, object] | None
    llm: dict[str, object]
    timings: dict[str, float]
    warnings: list[str]
    errors: list[str]
    rollback_status: str
    action_required: list[str]
