"""Stage 2 (structural checks) and stage 6 (semantic invariants)."""

from __future__ import annotations

import time

import libcst as cst

from codeshift.analysis.ast_analyzer import ModuleInfo, parse_module
from codeshift.analysis.callgraph import CallGraph
from codeshift.analysis.detectors import run_detectors
from codeshift.models.findings import TransformKind
from codeshift.models.migrations import MigrationPlan
from codeshift.models.verification import StageResult, StageStatus
from codeshift.rules.registry import AnalysisContext, Rule
from codeshift.transformations.async_migration import unawaited_calls


def _definitions(module: ModuleInfo) -> dict[str, list[str]]:
    """Qualified name -> parameter list for every function, plus classes with an empty list."""
    graph = CallGraph({module.path: module})
    out = {info.qualname: info.params for info in graph.functions.values()}
    return out


def interface_problems(before: ModuleInfo, after: ModuleInfo) -> list[str]:
    """Functions that vanished or whose parameters changed between two versions of a file."""
    if not (before.ok and after.ok):
        return []
    old, new = _definitions(before), _definitions(after)
    problems = []
    for name, params in sorted(old.items()):
        if name not in new:
            problems.append(f"{before.path}: {name}() no longer exists")
        elif new[name] != params:
            problems.append(
                f"{before.path}: {name}() parameters changed from {params} to {new[name]}"
            )
    return problems


def verify_structure(
    plan: MigrationPlan,
    originals: dict[str, str],
    candidates: dict[str, str],
    all_candidate_modules: dict[str, ModuleInfo],
    rules: list[Rule],
    drifted: list[str],
) -> tuple[StageResult, list[str]]:
    """Check that modified files are well formed and that the planned edits really happened.

    Returns the stage result and the list of semantic-invariant violations
    (interface changes, calls that were not awaited) for the review stage.
    """
    started = time.perf_counter()
    details: list[str] = []
    failed = False
    invariants: list[str] = []

    for path in drifted:
        failed = True
        details.append(f"{path}: content on disk differs from what the run recorded")
    planned = {change.file for change in plan.changes}
    unexpected = sorted(set(candidates) - planned)
    if unexpected:
        failed = True
        details.append(f"files outside the plan were modified: {', '.join(unexpected)}")

    modules_after: dict[str, ModuleInfo] = {}
    for path, source in sorted(candidates.items()):
        try:
            cst.parse_module(source)
        except (cst.ParserSyntaxError, RecursionError) as exc:
            failed = True
            details.append(f"{path}: LibCST cannot parse the modified file: {exc}")
            continue
        module = all_candidate_modules.get(path) or parse_module(path, source)
        if not module.ok:
            failed = True
            details.append(f"{path}: {module.error}")
            continue
        modules_after[path] = module
        if path in originals:
            invariants.extend(interface_problems(parse_module(path, originals[path]), module))

    # The findings a deterministic rule set out to fix must be gone.
    ctx = AnalysisContext(target_python=None, graph=None)
    file_rules = [rule for rule in rules if not rule.repo_wide]
    remaining = run_detectors(modules_after, file_rules, ctx)
    for change in plan.changes:
        for rule_id in change.rule_ids:
            before = sum(
                1
                for f in plan.findings
                if f.file == change.file
                and f.rule_id == rule_id
                and f.transform_kind is TransformKind.DETERMINISTIC
            )
            after = sum(
                1
                for f in remaining
                if f.file == change.file
                and f.rule_id == rule_id
                and f.transform_kind is TransformKind.DETERMINISTIC
            )
            if before and after >= before:
                failed = True
                details.append(
                    f"{change.file}: {rule_id} still reports {after} of {before} finding(s)"
                )

    if plan.converted_functions:
        graph = CallGraph({p: m for p, m in all_candidate_modules.items() if m.ok})
        invariants.extend(unawaited_calls(graph, set(plan.converted_functions)))

    if invariants:
        failed = True
        details.extend(invariants)
    if not failed:
        details.insert(0, f"{len(candidates)} file(s) reparsed with ast and LibCST")
        if plan.converted_functions:
            details.append(
                f"{len(plan.converted_functions)} converted function(s) are async, "
                "free of supported blocking calls and awaited at every visible call site"
            )
    return (
        StageResult(
            name="structural_checks",
            status=StageStatus.FAILED if failed else StageStatus.PASSED,
            details=details,
            duration_s=round(time.perf_counter() - started, 4),
        ),
        invariants,
    )


def semantic_review(
    plan: MigrationPlan, invariants: list[str], approved_by_user: bool, structure_ok: bool
) -> StageResult:
    """Stage 6: AI-assisted and async changes need invariants to hold and a human sign-off.

    No model is consulted here. Static invariants and tests gate correctness;
    approval records that a person looked at the diff.
    """
    reviewable = [c for c in plan.changes if c.origin == "llm" or c.requires_review]
    if not reviewable:
        return StageResult(
            name="semantic_review",
            status=StageStatus.SKIPPED,
            details=["no AI-assisted or review-required changes in this run"],
        )
    if invariants or not structure_ok:
        return StageResult(
            name="semantic_review",
            status=StageStatus.FAILED,
            details=["interface or await invariants are violated", *invariants],
        )
    files = ", ".join(sorted(c.file for c in reviewable))
    if approved_by_user:
        return StageResult(
            name="semantic_review",
            status=StageStatus.PASSED,
            details=[f"invariants hold and a reviewer approved the changes at apply time: {files}"],
        )
    return StageResult(
        name="semantic_review",
        status=StageStatus.REVIEW_REQUIRED,
        details=[f"invariants hold; a person still has to review: {files}"],
    )
