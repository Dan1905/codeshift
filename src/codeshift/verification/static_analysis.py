"""Stage 3: Ruff and mypy, compared against a baseline when one is available."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from codeshift.models.verification import StageResult, StageStatus

TOOL_TIMEOUT_SECONDS = 180
# Used when the project has no Ruff configuration: correctness problems only.
DEFAULT_RUFF_SELECT = "E9,F63,F7,F82,F401,F811"
_MYPY_LINE = re.compile(r"^(?P<file>[^:]+):\d+(?::\d+)?: error: (?P<message>.*)$")


@dataclass
class ToolRun:
    tool: str
    executed: bool
    exit_code: int | None = None
    diagnostics: Counter[tuple[str, str]] = field(default_factory=Counter)
    note: str = ""
    seconds: float = 0.0


def _has_ruff_config(tree: Path) -> bool:
    if (tree / "ruff.toml").is_file() or (tree / ".ruff.toml").is_file():
        return True
    pyproject = tree / "pyproject.toml"
    if pyproject.is_file():
        try:
            return "ruff" in tomllib.loads(pyproject.read_text("utf-8")).get("tool", {})
        except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
            return False
    return False


def _ruff_command() -> list[str] | None:
    executable = shutil.which("ruff") or shutil.which("ruff", path=str(Path(sys.executable).parent))
    return [executable] if executable else None


def run_ruff(tree: Path, files: list[str]) -> ToolRun:
    command = _ruff_command()
    if command is None:
        return ToolRun("ruff", executed=False, note="ruff is not installed")
    args = [*command, "check", "--output-format", "json", "--no-cache", "--force-exclude"]
    if not _has_ruff_config(tree):
        args += ["--isolated", "--select", DEFAULT_RUFF_SELECT]
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [*args, *files],
            cwd=tree,
            capture_output=True,
            text=True,
            timeout=TOOL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ToolRun("ruff", executed=False, note=f"ruff could not run: {exc}")
    run = ToolRun(
        "ruff", True, completed.returncode, seconds=round(time.perf_counter() - started, 3)
    )
    try:
        items = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError:
        run.executed = False
        run.note = f"ruff output could not be parsed: {completed.stderr.strip()[:200]}"
        return run
    for item in items:
        try:
            rel = Path(item["filename"]).resolve().relative_to(tree.resolve()).as_posix()
        except ValueError:
            rel = str(item.get("filename"))
        run.diagnostics[(rel, f"{item.get('code') or 'syntax'}: {item.get('message')}")] += 1
    return run


def run_mypy(tree: Path, files: list[str]) -> ToolRun:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "mypy", "--no-incremental", "--no-error-summary", *files],
            cwd=tree,
            capture_output=True,
            text=True,
            timeout=TOOL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ToolRun("mypy", executed=False, note=f"mypy could not run: {exc}")
    if "No module named mypy" in completed.stderr:
        return ToolRun("mypy", executed=False, note="mypy is not installed")
    run = ToolRun(
        "mypy", True, completed.returncode, seconds=round(time.perf_counter() - started, 3)
    )
    for line in completed.stdout.splitlines():
        match = _MYPY_LINE.match(line)
        if match:
            run.diagnostics[(match["file"], match["message"])] += 1
    return run


def verify_static(
    candidate: Path,
    baseline: Path | None,
    files: list[str],
    use_ruff: bool,
    use_mypy: bool,
) -> tuple[StageResult, list[str]]:
    """Run the enabled tools. Returns the stage and a list of newly introduced diagnostics."""
    if not use_ruff and not use_mypy:
        return (
            StageResult(
                name="static_analysis",
                status=StageStatus.SKIPPED,
                details=["static analysis is disabled in configuration"],
                mandatory=False,
            ),
            [],
        )
    started = time.perf_counter()
    details: list[str] = []
    introduced: list[str] = []
    not_run = False
    exit_codes: list[int] = []
    runners = [(use_ruff, run_ruff), (use_mypy, run_mypy)]
    for enabled, runner in runners:
        if not enabled:
            continue
        after = runner(candidate, files)
        if not after.executed:
            not_run = True
            details.append(f"{after.tool}: not executed ({after.note})")
            continue
        exit_codes.append(after.exit_code or 0)
        total = sum(after.diagnostics.values())
        if baseline is None:
            new = after.diagnostics
            details.append(f"{after.tool}: {total} diagnostic(s), no baseline to compare with")
        else:
            before = runner(baseline, files)
            if not before.executed:
                new = after.diagnostics
                details.append(f"{after.tool}: baseline run failed ({before.note})")
            else:
                new = after.diagnostics - before.diagnostics
                details.append(
                    f"{after.tool}: {total} diagnostic(s) after, "
                    f"{sum(before.diagnostics.values())} before, {sum(new.values())} new "
                    f"({after.seconds}s)"
                )
        for (path, message), count in sorted(new.items()):
            introduced.append(
                f"{after.tool}: {path}: {message}" + (f" (x{count})" if count > 1 else "")
            )
    details.extend(introduced)
    if introduced:
        status = StageStatus.FAILED
    elif not_run:
        status = StageStatus.NOT_RUN
    else:
        status = StageStatus.PASSED
    return (
        StageResult(
            name="static_analysis",
            status=status,
            details=details,
            duration_s=round(time.perf_counter() - started, 4),
            exit_code=max(exit_codes) if exit_codes else None,
        ),
        introduced,
    )
