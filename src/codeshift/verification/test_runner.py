"""Stages 4 and 5: run the project's tests in a disposable copy and compare with a baseline.

Tests execute repository code, so they only run when explicitly enabled, always
in a temporary copy of the repository, with a timeout, and optionally inside a
network-less Docker container.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from codeshift.config import VerificationConfig
from codeshift.discovery.manifest import Manifest
from codeshift.models.verification import StageResult, StageStatus

JUNIT_NAME = ".codeshift-junit.xml"
PYTEST_NO_TESTS_COLLECTED = 5
OUTPUT_TAIL_CHARS = 2000


@dataclass
class TestRun:
    __test__ = False  # not a pytest test class

    executed: bool
    exit_code: int | None = None
    passed: int = 0
    failed: set[str] = field(default_factory=set)
    note: str = ""
    output_tail: str = ""
    seconds: float = 0.0
    per_test_results: bool = False


def discover_test_command(manifest: Manifest, config: VerificationConfig) -> list[str] | None:
    """Pick the test command: the configured one, else pytest when the repository has tests."""
    if config.test_command:
        return list(config.test_command)
    has_tests = bool(manifest.test_dirs) or any(f.is_test for f in manifest.files)
    if has_tests:
        return ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    return None


def _materialise(command: list[str], tree: Path, config: VerificationConfig) -> list[str]:
    is_pytest = "pytest" in " ".join(command[:3])
    if is_pytest:
        command = [*command, f"--junitxml={JUNIT_NAME}", "-o", "junit_family=xunit2"]
    if config.isolation == "docker":
        return [
            "docker", "run", "--rm", "--network", "none", "--memory", "1g", "--cpus", "2",
            "--pids-limit", "256", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONPATH=/workspace",
            "-v", f"{tree}:/workspace", "-w", "/workspace", "--entrypoint", command[0],
            config.docker_image, *command[1:],
        ]  # fmt: skip
    if command[0] == "python":
        command = [sys.executable, *command[1:]]
    return command


def _parse_junit(path: Path) -> tuple[int, set[str]] | None:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    passed = 0
    failed: set[str] = set()
    for case in root.iter("testcase"):
        identifier = f"{case.get('classname', '')}::{case.get('name', '')}"
        if case.find("failure") is not None or case.find("error") is not None:
            failed.add(identifier)
        elif case.find("skipped") is None:
            passed += 1
    return passed, failed


def run_tests(tree: Path, command: list[str], config: VerificationConfig) -> TestRun:
    """Run `command` inside `tree` (a disposable copy) and collect per-test results."""
    if config.isolation == "docker" and shutil.which("docker") is None:
        return TestRun(
            executed=False, note="docker isolation requested but docker is not installed"
        )
    full = _materialise(command, tree, config)
    env = {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD"))
    }
    env.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(tree)})
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            full,
            cwd=tree,
            env=env,
            capture_output=True,
            text=True,
            timeout=config.test_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return TestRun(
            executed=False,
            note=f"tests exceeded the {config.test_timeout_seconds}s timeout",
            seconds=round(time.perf_counter() - started, 3),
        )
    except OSError as exc:
        return TestRun(executed=False, note=f"test command could not start: {exc}")
    run = TestRun(
        executed=True,
        exit_code=completed.returncode,
        output_tail=(completed.stdout + completed.stderr)[-OUTPUT_TAIL_CHARS:],
        seconds=round(time.perf_counter() - started, 3),
    )
    if config.isolation == "docker" and completed.returncode == 125:
        return TestRun(
            executed=False, note=f"docker could not start: {completed.stderr.strip()[-300:]}"
        )
    junit = _parse_junit(tree / JUNIT_NAME)
    if junit is not None:
        run.passed, run.failed = junit
        run.per_test_results = True
    if completed.returncode == PYTEST_NO_TESTS_COLLECTED and junit is not None and not junit[0]:
        run.executed = False
        run.note = "no tests were collected"
    return run


def verify_tests(
    candidate: Path,
    baseline: Path | None,
    manifest: Manifest,
    config: VerificationConfig,
    enabled: bool,
) -> tuple[StageResult, StageResult]:
    """Return the unit-test stage and the regression stage."""
    mandatory = config.require_tests and config.fail_on_test_failure
    if not enabled:
        note = "tests were not executed (enable with --run-tests or verification.run_tests)"
        return (
            StageResult(
                name="unit_tests", status=StageStatus.NOT_RUN, details=[note], mandatory=mandatory
            ),
            StageResult(
                name="regression",
                status=StageStatus.NOT_RUN,
                details=["no test results to compare"],
                mandatory=False,
            ),
        )
    command = discover_test_command(manifest, config)
    if command is None:
        note = "no test command configured and no tests discovered"
        return (
            StageResult(
                name="unit_tests", status=StageStatus.NOT_RUN, details=[note], mandatory=mandatory
            ),
            StageResult(
                name="regression", status=StageStatus.NOT_RUN, details=[note], mandatory=False
            ),
        )

    after = run_tests(candidate, command, config)
    label = " ".join(command)
    if not after.executed:
        return (
            StageResult(
                name="unit_tests",
                status=StageStatus.NOT_RUN,
                details=[f"`{label}`: {after.note}"],
                duration_s=after.seconds,
                mandatory=mandatory,
            ),
            StageResult(
                name="regression",
                status=StageStatus.NOT_RUN,
                details=["no test results to compare"],
                mandatory=False,
            ),
        )

    tests_failed = after.exit_code != 0
    details = [
        f"`{label}` exited with {after.exit_code} in {after.seconds}s"
        + (f": {after.passed} passed, {len(after.failed)} failed" if after.per_test_results else "")
    ]
    details.extend(f"failed: {name}" for name in sorted(after.failed))
    if tests_failed and not after.failed:
        details.append("output: " + after.output_tail.strip()[-600:])

    regression = StageResult(
        name="regression", status=StageStatus.NOT_RUN, details=[], mandatory=False
    )
    blocking = config.fail_on_test_failure
    if baseline is None:
        regression.details.append("baseline comparison is disabled or unavailable")
    else:
        before = run_tests(baseline, command, config)
        regression.duration_s = before.seconds
        if not before.executed:
            regression.details.append(f"baseline tests could not run: {before.note}")
        elif not (before.per_test_results and after.per_test_results):
            baseline_failed = before.exit_code != 0
            regression.mandatory = config.fail_on_test_failure
            if tests_failed and not baseline_failed:
                regression.status = StageStatus.FAILED
                regression.details.append("tests pass on the baseline and fail after the migration")
            elif tests_failed:
                regression.details.append(
                    "tests fail both before and after; without per-test results "
                    "new failures cannot be told apart from existing ones"
                )
            else:
                regression.status = StageStatus.PASSED
                regression.details.append("tests pass after the migration")
        else:
            introduced = sorted(after.failed - before.failed)
            existing = sorted(after.failed & before.failed)
            regression.mandatory = config.fail_on_test_failure
            regression.details.append(
                f"baseline: {before.passed} passed, {len(before.failed)} failed; "
                f"after: {after.passed} passed, {len(after.failed)} failed"
            )
            regression.details.extend(f"introduced by the migration: {name}" for name in introduced)
            regression.details.extend(f"already failing before: {name}" for name in existing)
            if introduced or (tests_failed and not after.failed and before.exit_code == 0):
                regression.status = StageStatus.FAILED
            else:
                regression.status = StageStatus.PASSED
                if tests_failed and existing:
                    # Every failure also fails without the migration: report it, do not blame it.
                    blocking = False
                    details.append("all failures are pre-existing (they also fail on the baseline)")
    unit = StageResult(
        name="unit_tests",
        status=StageStatus.FAILED if tests_failed else StageStatus.PASSED,
        details=details,
        duration_s=after.seconds,
        exit_code=after.exit_code,
        mandatory=blocking if tests_failed else mandatory,
    )
    return unit, regression
