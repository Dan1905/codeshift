"""Run rule detectors over parsed modules and deduplicate their findings."""

from __future__ import annotations

from codeshift.analysis.ast_analyzer import ModuleInfo
from codeshift.models.findings import Finding
from codeshift.rules.registry import AnalysisContext, Rule


def run_detectors(
    modules: dict[str, ModuleInfo], rules: list[Rule], ctx: AnalysisContext
) -> list[Finding]:
    """Collect findings from every enabled rule for every successfully parsed module."""
    findings: list[Finding] = []
    for path in sorted(modules):
        module = modules[path]
        if not module.ok:
            continue
        for rule in rules:
            findings.extend(rule.detect(module, ctx))
    return deduplicate(findings)


def deduplicate(findings: list[Finding]) -> list[Finding]:
    """Drop exact repeats and findings fully covered by a same-rule finding on the same lines."""
    seen: dict[str, Finding] = {}
    for finding in findings:
        seen.setdefault(finding.id, finding)
    unique = sorted(seen.values(), key=lambda f: (f.file, f.start_line, f.column, f.rule_id))
    result: list[Finding] = []
    for finding in unique:
        duplicate = any(
            other.rule_id == finding.rule_id
            and other.file == finding.file
            and other.start_line == finding.start_line
            and other.end_line == finding.end_line
            and other.column == finding.column
            for other in result
        )
        if not duplicate:
            result.append(finding)
    return result


def count_by_rule(findings: list[Finding]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.rule_id] = counts.get(finding.rule_id, 0) + 1
    return dict(sorted(counts.items()))
