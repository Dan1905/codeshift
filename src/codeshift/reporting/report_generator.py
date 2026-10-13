"""Build terminal and JSON reports from a persisted run. All numbers come from the record."""

from __future__ import annotations

from codeshift.analysis.detectors import count_by_rule
from codeshift.models.migrations import ChangeStatus, RunStatus
from codeshift.models.reports import Report, RunRecord
from codeshift.models.verification import OverallStatus, StageStatus

STAGE_LABELS = {
    "syntax": "Syntax checks",
    "structural_checks": "Structural checks",
    "static_analysis": "Static analysis",
    "unit_tests": "Unit tests",
    "regression": "Regression check",
    "semantic_review": "Semantic review",
}
STATUS_LABELS = {
    StageStatus.PASSED: "PASS",
    StageStatus.FAILED: "FAIL",
    StageStatus.SKIPPED: "SKIPPED",
    StageStatus.NOT_RUN: "NOT RUN",
    StageStatus.REVIEW_REQUIRED: "REVIEW REQUIRED",
}


def _actions(record: RunRecord) -> list[str]:
    run_id = record.run_id
    actions = []
    verification = record.verification
    if record.status is RunStatus.PLANNED and record.plan.changes:
        actions.append(
            f"Review `codeshift diff --run-id {run_id}`, then apply or discard the plan."
        )
    if verification is not None:
        if verification.overall_status is OverallStatus.FAILED:
            failed = [
                s.name
                for s in verification.stages
                if s.status is StageStatus.FAILED and s.mandatory
            ]
            actions.append(f"Inspect the failing checks ({', '.join(failed)}) before accepting.")
            if record.status is RunStatus.FAILED:
                actions.append(f"Undo with `codeshift rollback --run-id {run_id}` if needed.")
        elif verification.overall_status is OverallStatus.INCOMPLETE:
            missing = [
                s.name
                for s in verification.stages
                if s.status is StageStatus.NOT_RUN and s.mandatory
            ]
            actions.append(
                f"Verification is incomplete; not executed: {', '.join(missing)}. "
                "Run `codeshift verify --run-tests`."
            )
        elif verification.overall_status is OverallStatus.REVIEW_REQUIRED:
            actions.append("A person must review the AI-assisted or high-risk changes.")
    elif record.status is RunStatus.APPLIED:
        actions.append(f"Changes are applied but unverified: `codeshift verify --run-id {run_id}`.")
    if record.plan.proposals:
        actions.append(
            f"{len(record.plan.proposals)} conversion group(s) were not changed and need manual work."
        )
    if record.plan.parse_failures:
        actions.append(
            f"{len(record.plan.parse_failures)} file(s) could not be parsed and were not analysed."
        )
    return actions


def build_report(record: RunRecord) -> Report:
    plan = record.plan
    modified = [c for c in plan.changes if c.status is ChangeStatus.APPLIED]
    verification: dict[str, object] | None = None
    if record.verification is not None:
        verification = {
            "summary": record.verification.summary(),
            "target": record.verification.target,
            "stages": [stage.model_dump(mode="json") for stage in record.verification.stages],
        }
    return Report(
        run_id=plan.run_id,
        repo=plan.repo,
        status=record.status.value,
        created_at=plan.created_at,
        updated_at=record.updated_at,
        rules=plan.rules,
        files_analyzed=plan.files_analyzed,
        files_modified=len(modified),
        transformations=sum(len(c.rule_ids) for c in modified),
        findings_by_rule=count_by_rule(plan.findings),
        changes=[c.model_dump(mode="json", exclude={"diff"}) for c in plan.changes],
        groups=[g.model_dump(mode="json") for g in plan.groups],
        proposals=[p.model_dump(mode="json") for p in plan.proposals],
        verification=verification,
        llm=plan.llm.model_dump(mode="json"),
        timings=plan.timings.model_dump(),
        warnings=plan.warnings,
        errors=record.errors,
        rollback_status=record.rollback_status,
        action_required=_actions(record),
    )


def render_text(record: RunRecord) -> str:
    """Plain-text migration summary."""
    report = build_report(record)
    plan = record.plan
    lines = [
        "Migration Summary",
        "-----------------",
        f"Run ID: {report.run_id}",
        f"Repository: {report.repo}",
        f"Status: {report.status}",
        f"Files analyzed: {report.files_analyzed}",
        f"Files with proposed changes: {len(plan.changes)}",
        f"Files modified: {report.files_modified}",
        f"Transformations applied: {report.transformations}",
        f"Findings: {len(plan.findings)}",
    ]
    if plan.llm.requests:
        lines.append(
            f"LLM: {plan.llm.provider} / {plan.llm.model}, {plan.llm.requests} request(s), "
            f"{plan.llm.failed_requests} rejected or failed"
        )
    if plan.groups:
        converted = sum(1 for g in plan.groups if g.converted)
        lines.append(
            f"Async conversion groups: {converted} converted, {len(plan.groups) - converted} left as proposals"
        )
        lines.append(f"Functions converted to async: {len(plan.converted_functions)}")
    lines.append("")
    lines.append("Verification:")
    if record.verification is None:
        lines.append("  not run")
        overall = "NOT VERIFIED"
    else:
        for stage in record.verification.stages:
            label = STAGE_LABELS.get(stage.name, stage.name)
            suffix = "" if stage.mandatory else " (informational)"
            lines.append(f"  {label}: {STATUS_LABELS[stage.status]}{suffix}")
            for detail in stage.details[:8]:
                lines.append(f"      {detail}")
        overall = record.verification.overall_status.value.upper().replace("_", " ")
        lines.append(f"  Checked: {record.verification.target.replace('_', ' ')}")
    lines += ["", f"Overall status: {overall}"]
    if record.rollback_status != "not_requested":
        lines.append(f"Rollback: {record.rollback_status}")
    if plan.warnings:
        lines += ["", "Warnings:"] + [f"  - {w}" for w in plan.warnings]
    if record.errors:
        lines += ["", "Errors:"] + [f"  - {e}" for e in record.errors]
    if report.action_required:
        lines += ["", "Action required:"] + [f"  {a}" for a in report.action_required]
    timings = plan.timings
    lines += [
        "",
        f"Timings: scan {timings.scan_s}s, analysis {timings.analysis_s}s, "
        f"transform {timings.transform_s}s, verification {timings.verification_s}s",
    ]
    return "\n".join(lines)
