"""Stage 1: syntax verification of modified files."""

from __future__ import annotations

import ast
import time

from codeshift.models.verification import StageResult, StageStatus


def verify_syntax(sources: dict[str, str], target_python: str | None = None) -> StageResult:
    """Parse every modified file, optionally against the target version's grammar."""
    started = time.perf_counter()
    details: list[str] = []
    failed = False
    feature = (3, int(target_python.split(".")[1])) if target_python else None
    for path, source in sorted(sources.items()):
        try:
            ast.parse(source, filename=path)
        except (SyntaxError, ValueError, RecursionError) as exc:
            failed = True
            details.append(
                f"{path}: {getattr(exc, 'msg', exc)} (line {getattr(exc, 'lineno', '?')})"
            )
            continue
        if feature:
            try:
                ast.parse(source, filename=path, feature_version=feature)
            except SyntaxError as exc:
                failed = True
                details.append(
                    f"{path}: not valid Python {target_python}: {exc.msg} (line {exc.lineno})"
                )
            except ValueError:
                details.append(f"{path}: target version {target_python} could not be checked")
    if not failed:
        details.insert(0, f"{len(sources)} modified file(s) parsed")
    return StageResult(
        name="syntax",
        status=StageStatus.FAILED if failed else StageStatus.PASSED,
        details=details,
        duration_s=round(time.perf_counter() - started, 4),
    )
