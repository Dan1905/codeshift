"""Apply deterministic, file-local rules in memory using LibCST."""

from __future__ import annotations

import logging

from codeshift.analysis.ast_analyzer import parse_module
from codeshift.discovery.manifest import sha256_text
from codeshift.exceptions import TransformError
from codeshift.models.migrations import ChangeLocation, TransformationResult, TransformStatus
from codeshift.rules.registry import Rule
from codeshift.transformations.base import locations_for, syntax_error

log = logging.getLogger("codeshift.transform")


def apply_rules(path: str, source: str, rules: list[Rule]) -> TransformationResult:
    """Run each rule's rewrite over `source` in order and report what changed.

    The file on disk is not touched. A rule whose output does not parse is
    discarded and reported instead of being passed on.
    """
    current = source
    applied: list[str] = []
    warnings: list[str] = []
    locations: list[ChangeLocation] = []
    requirements: set[str] = set()
    requires_review = False
    failed = False

    for rule in rules:
        if rule.repo_wide:
            continue
        module = parse_module(path, current)
        if not module.ok:
            warnings.append(f"{path}: cannot transform, {module.error}")
            failed = True
            break
        try:
            outcome = rule.transform(module)
        except TransformError as exc:
            warnings.append(f"{path}: {rule.spec.id} failed: {exc}")
            failed = True
            continue
        if outcome is None:
            continue
        warnings.extend(f"{path}: {rule.spec.id}: {w}" for w in outcome.warnings)
        if outcome.count == 0 or outcome.source == current:
            continue
        error = syntax_error(outcome.source, path)
        if error:
            warnings.append(
                f"{path}: {rule.spec.id} produced invalid Python and was discarded: {error}"
            )
            failed = True
            continue
        locations.extend(locations_for(rule.spec.id, current, outcome.source))
        current = outcome.source
        applied.append(rule.spec.id)
        requirements.update(rule.spec.verification)
        requires_review = requires_review or rule.spec.requires_review
        log.debug(
            "rule applied", extra={"rule": rule.spec.id, "file": path, "count": outcome.count}
        )

    if current != source:
        status = TransformStatus.PROPOSED
    elif failed:
        status = TransformStatus.FAILED
    else:
        status = TransformStatus.UNCHANGED
    return TransformationResult(
        file=path,
        original_hash=sha256_text(source),
        proposed_hash=sha256_text(current),
        modified_source=current,
        applied_rules=applied,
        locations=locations,
        warnings=warnings,
        verification_requirements=sorted(requirements),
        status=status,
        requires_review=requires_review,
        origin="deterministic",
    )
