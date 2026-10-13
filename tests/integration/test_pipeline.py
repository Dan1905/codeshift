"""Full pipeline runs against real temporary repositories (no external LLM)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeshift.config import Config
from codeshift.exceptions import StaleSourceError, ValidationError
from codeshift.models.migrations import RunStatus
from codeshift.models.verification import OverallStatus, StageStatus
from codeshift.orchestration.migration_runner import MigrationRunner
from tests.conftest import FIXTURES, ScriptedProvider

EXPECTED_SERVICE = FIXTURES / "expected" / "legacy_service"

CLIENT = """import time


def wait(seconds):
    time.sleep(seconds)
    return seconds
"""


def snapshot(repo: Path) -> dict[str, bytes]:
    return {
        p.relative_to(repo).as_posix(): p.read_bytes()
        for p in sorted(repo.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


def config(**verification) -> Config:
    return Config.model_validate({"verification": {"run_ruff": False, **verification}})


def test_plan_matches_expected_files_and_leaves_the_repository_untouched(service_repo, store):
    before = snapshot(service_repo)
    runner = MigrationRunner(service_repo, store=store)
    record = runner.plan(llm_mode="sample")

    assert snapshot(service_repo) == before, "planning must not modify source files"
    proposed = {
        change.file: store.artifacts.read(record.run_id, "proposed", change.file).decode()
        for change in record.plan.changes
    }
    expected = {
        p.relative_to(EXPECTED_SERVICE).as_posix(): p.read_text()
        for p in EXPECTED_SERVICE.rglob("*.py")
    }
    assert proposed == expected
    # Files without supported patterns are not part of the plan.
    assert "app/settings.py" not in proposed and "tests/conftest.py" not in proposed
    by_file = {c.file: c for c in record.plan.changes}
    assert (
        by_file["app/main.py"].requires_review and by_file["app/main.py"].origin == "deterministic"
    )
    assert by_file["app/inventory_client.py"].origin == "llm"
    assert by_file["tools/export_report.py"].requires_review is False
    assert all(c.diff.startswith(f"--- a/{c.file}") for c in record.plan.changes)
    # Two functions in the tools script cannot be converted safely and stay proposals.
    assert sorted(len(g.blockers) > 0 for g in record.plan.groups) == [False, True, True]
    assert store.get(record.run_id).status is RunStatus.PLANNED


def test_apply_verify_rollback_round_trip_with_real_tests(service_repo, store):
    before = snapshot(service_repo)
    runner = MigrationRunner(service_repo, store=store)
    record = runner.plan(llm_mode="sample")

    with pytest.raises(ValidationError, match="require review"):
        runner.apply(record.run_id, approved=False)
    assert snapshot(service_repo) == before

    runner.apply(record.run_id, approved=True)
    assert (service_repo / "app/main.py").read_text() == (
        EXPECTED_SERVICE / "app/main.py"
    ).read_text()

    verified = runner.verify(record.run_id, run_tests=True)
    assert verified.verification is not None
    summary = verified.verification.summary()
    assert summary == {
        "syntax": "passed",
        "structural_checks": "passed",
        "static_analysis": "passed",
        "unit_tests": "passed",
        "regression": "passed",
        "semantic_review": "passed",
        "overall_status": "passed",
    }
    assert verified.status is RunStatus.VERIFIED

    rolled_back = runner.rollback(record.run_id)
    assert rolled_back.status is RunStatus.ROLLED_BACK
    assert snapshot(service_repo) == before


def test_verification_without_tests_is_incomplete_not_passed(make_repo, store):
    repo = make_repo(
        {"client.py": CLIENT, "tests/test_client.py": "def test_ok():\n    assert True\n"}
    )
    runner = MigrationRunner(repo, config(), store)
    record = runner.plan()
    runner.apply(record.run_id, approved=True)
    verified = runner.verify(record.run_id, run_tests=False)
    assert verified.verification is not None
    assert verified.verification.stage("unit_tests").status is StageStatus.NOT_RUN
    assert verified.verification.overall_status is OverallStatus.INCOMPLETE
    assert verified.status is RunStatus.APPLIED


def test_failing_tests_prevent_success_and_are_attributed_to_the_migration(make_repo, store):
    repo = make_repo(
        {
            "client.py": CLIENT,
            # Calls the function through a path the engine cannot see, so it is not awaited.
            "tests/test_client.py": (
                "import importlib\n\n\n"
                "def test_wait_returns_value():\n"
                "    assert importlib.import_module('client').wait(0) == 0\n\n\n"
                "def test_unrelated():\n    assert 1 + 1 == 2\n"
            ),
        }
    )
    runner = MigrationRunner(repo, config(), store)
    record = runner.plan()
    runner.apply(record.run_id, approved=True)
    verified = runner.verify(record.run_id, run_tests=True)
    result = verified.verification
    assert result is not None
    assert result.stage("unit_tests").status is StageStatus.FAILED
    regression = result.stage("regression")
    assert regression.status is StageStatus.FAILED
    assert any(
        "introduced by the migration" in d and "test_wait_returns_value" in d
        for d in regression.details
    )
    assert result.overall_status is OverallStatus.FAILED
    assert verified.status is RunStatus.FAILED


def test_preexisting_failures_are_distinguished_from_new_ones(make_repo, store):
    repo = make_repo(
        {
            "client.py": CLIENT,
            "tests/test_client.py": "def test_already_broken():\n    assert False\n",
        }
    )
    runner = MigrationRunner(repo, config(), store)
    record = runner.plan()
    verified = runner.verify(record.run_id, run_tests=True)  # verified before applying
    result = verified.verification
    assert result is not None and result.target == "proposed_copy"
    unit = result.stage("unit_tests")
    assert unit.status is StageStatus.FAILED and unit.mandatory is False
    regression = result.stage("regression")
    assert regression.status is StageStatus.PASSED
    assert any("already failing before" in d for d in regression.details)
    assert result.overall_status is not OverallStatus.FAILED
    assert (repo / "client.py").read_text() == CLIENT  # verifying a plan writes nothing


def test_syntax_errors_block_successful_completion(make_repo, store):
    repo = make_repo({"client.py": CLIENT})
    runner = MigrationRunner(repo, config(), store)
    record = runner.plan()
    runner.apply(record.run_id, approved=True)
    (repo / "client.py").write_text("async def wait(seconds:\n    return\n")
    verified = runner.verify(record.run_id, run_tests=False)
    result = verified.verification
    assert result is not None
    assert result.stage("syntax").status is StageStatus.FAILED
    assert result.stage("structural_checks").status is StageStatus.FAILED
    assert result.overall_status is OverallStatus.FAILED
    assert verified.status is RunStatus.FAILED


def test_invalid_llm_output_never_becomes_a_change(make_repo, store):
    repo = make_repo(
        {"client.py": "import requests\n\n\ndef fetch(url):\n    return requests.get(url).json()\n"}
    )
    hostile = json.dumps(
        {
            "migration_id": "mig_wrong",
            "changes": [
                {
                    "file": "client.py",
                    "operation": "replace_function",
                    "target_symbol": "fetch",
                    "replacement_code": "async def fetch(url):\n    import os\n    os.system('id')\n",
                }
            ],
        }
    )
    provider = ScriptedProvider([hostile, "I refuse to answer in JSON"])
    runner = MigrationRunner(repo, config(), store, provider=provider)
    record = runner.plan()

    assert record.plan.changes == []
    assert record.plan.converted_functions == []
    assert "provider failed" in record.plan.groups[0].blockers[0]
    assert record.plan.llm.requests == 2 and record.plan.llm.failed_requests == 2
    assert len(provider.requests) == 2 and provider.feedback[1]
    with pytest.raises(ValidationError, match="no proposed changes"):
        runner.apply(record.run_id, approved=True)


def test_user_edits_after_planning_are_preserved(make_repo, store):
    repo = make_repo(
        {"client.py": CLIENT, "other.py": "import time\n\n\ndef nap():\n    time.sleep(1)\n"}
    )
    runner = MigrationRunner(repo, config(), store)
    record = runner.plan()
    edited = CLIENT + "\n# important local change\n"
    (repo / "client.py").write_text(edited)

    with pytest.raises(StaleSourceError, match="client.py"):
        runner.apply(record.run_id, approved=True)
    assert (repo / "client.py").read_text() == edited
    assert "time.sleep(1)" in (repo / "other.py").read_text(), "no file may be partially applied"
    assert store.get(record.run_id).status is RunStatus.PLANNED
    assert any("apply refused" in e for e in store.get(record.run_id).errors)


def test_rollback_refuses_to_overwrite_later_changes(make_repo, store):
    repo = make_repo({"client.py": CLIENT})
    runner = MigrationRunner(repo, config(), store)
    record = runner.plan()
    runner.apply(record.run_id, approved=True)
    later = (repo / "client.py").read_text() + "\nNEW_CONSTANT = 1\n"
    (repo / "client.py").write_text(later)

    with pytest.raises(StaleSourceError):
        runner.rollback(record.run_id)
    assert (repo / "client.py").read_text() == later
    assert store.get(record.run_id).rollback_status == "refused"


def test_low_risk_changes_need_approval_unless_auto_apply_is_enabled(make_repo, store):
    files = {"a.py": "try:\n    pass\nexcept IOError:\n    pass\n"}
    repo = make_repo(files)
    runner = MigrationRunner(repo, config(), store)
    record = runner.plan()
    assert [c.requires_review for c in record.plan.changes] == [False]
    with pytest.raises(ValidationError, match="explicit approval"):
        runner.apply(record.run_id, approved=False)

    auto = Config.model_validate({"migrations": {"auto_apply_low_risk": True}})
    auto_runner = MigrationRunner(repo, auto, store)
    auto_runner.apply(auto_runner.plan().run_id, approved=False)
    assert "except OSError:" in (repo / "a.py").read_text()


def test_partial_repository_failures_are_reported_not_fatal(make_repo, store):
    repo = make_repo(
        {"good.py": "try:\n    pass\nexcept IOError:\n    pass\n", "broken.py": "def (:\n"}
    )
    runner = MigrationRunner(repo, config(), store)
    analysis = runner.analyze()
    assert [f.path for f in analysis.manifest.parse_failures] == ["broken.py"]
    assert [f.rule_id for f in analysis.findings] == ["python.oserror-aliases"]
    record = runner.plan()
    assert record.plan.parse_failures and [c.file for c in record.plan.changes] == ["good.py"]


def test_incremental_analysis_reuses_cached_findings_until_a_file_changes(make_repo, store):
    repo = make_repo({"a.py": "try:\n    pass\nexcept IOError:\n    pass\n", "b.py": "x = 1\n"})
    runner = MigrationRunner(repo, config(), store)
    first = runner.analyze(["python-modernization"])
    second = runner.analyze(["python-modernization"])
    assert (first.cache_hits, second.cache_hits) == (0, 2)
    assert [f.id for f in first.findings] == [f.id for f in second.findings]
    (repo / "b.py").write_text("try:\n    pass\nexcept EnvironmentError:\n    pass\n")
    third = runner.analyze(["python-modernization"])
    assert third.cache_hits == 1 and len(third.findings) == 2
