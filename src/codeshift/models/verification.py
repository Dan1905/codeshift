"""Structured verification results and the aggregation policy."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class StageStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    # Stage does not apply to this run (for example no AI-assisted changes).
    SKIPPED = "skipped"
    # Stage applies but was not executed. Never treated as a pass.
    NOT_RUN = "not_run"
    REVIEW_REQUIRED = "review_required"


class OverallStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    REVIEW_REQUIRED = "review_required"


STAGES = (
    "syntax",
    "structural_checks",
    "static_analysis",
    "unit_tests",
    "regression",
    "semantic_review",
)


class StageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    status: StageStatus
    details: list[str] = Field(default_factory=list)
    duration_s: float = 0.0
    exit_code: int | None = None
    mandatory: bool = True


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stages: list[StageResult] = Field(default_factory=list)
    overall_status: OverallStatus = OverallStatus.INCOMPLETE
    target: str = "working_tree"

    def stage(self, name: str) -> StageResult | None:
        return next((s for s in self.stages if s.name == name), None)

    def summary(self) -> dict[str, str]:
        out = {s.name: s.status.value for s in self.stages}
        out["overall_status"] = self.overall_status.value
        return out


def aggregate(stages: list[StageResult]) -> OverallStatus:
    """Combine stage results.

    A failed mandatory stage fails the run. A mandatory stage that is missing or
    was not executed makes the run incomplete, so unexecuted checks never count
    as passing. Non-mandatory stages (for example tests whose only failures also
    fail on the baseline) are reported but do not decide the outcome.
    """
    by_name = {s.name: s for s in stages}
    if any(s.status is StageStatus.FAILED and s.mandatory for s in stages):
        return OverallStatus.FAILED
    for name in ("syntax", "structural_checks"):
        if name not in by_name:
            return OverallStatus.INCOMPLETE
    if any(s.mandatory and s.status is StageStatus.NOT_RUN for s in stages):
        return OverallStatus.INCOMPLETE
    if any(s.status is StageStatus.REVIEW_REQUIRED for s in stages):
        return OverallStatus.REVIEW_REQUIRED
    return OverallStatus.PASSED
