from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codeshift.discovery.manifest import sha256_text
from codeshift.exceptions import RunNotFoundError, StaleSourceError, ValidationError
from codeshift.git.diff import dirty_files, unified_diff
from codeshift.git.rollback import apply_changes, rollback_changes
from codeshift.models.migrations import ChangeStatus, FileChange, MigrationPlan, RunStatus
from codeshift.models.reports import RunRecord
from codeshift.models.verification import (
    OverallStatus,
    StageResult,
    StageStatus,
    aggregate,
)
from codeshift.storage.database import ArtifactStore, RunStore
from codeshift.verification.syntax import verify_syntax
from codeshift.verification.test_runner import _parse_junit

ORIGINAL = "x = 1\n"
PROPOSED = "x = 2\n"


def change_for(path: str = "a.py") -> FileChange:
    return FileChange(
        file=path,
        original_hash=sha256_text(ORIGINAL),
        proposed_hash=sha256_text(PROPOSED),
        rule_ids=["rule"],
        diff=unified_diff(path, ORIGINAL, PROPOSED),
    )


def record_for(repo: Path, run_id: str = "run_1") -> RunRecord:
    plan = MigrationPlan(
        run_id=run_id, repo=str(repo), created_at="2026-01-01T00:00:00+00:00", rules=["rule"]
    )
    plan.changes.append(change_for())
    return RunRecord(plan=plan, updated_at=plan.created_at)


@pytest.fixture()
def prepared(tmp_path: Path, store: RunStore):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text(ORIGINAL)
    record = record_for(repo)
    store.artifacts.write("run_1", "proposed", "a.py", PROPOSED.encode())
    return repo, record, store


def test_unified_diff_format():
    diff = unified_diff("pkg/a.py", "a = 1\nb = 2\n", "a = 1\nb = 3\n")
    assert diff.splitlines()[:2] == ["--- a/pkg/a.py", "+++ b/pkg/a.py"]
    assert "-b = 2" in diff and "+b = 3" in diff
    assert unified_diff("a.py", "same\n", "same\n") == ""
    assert "No newline at end of file" in unified_diff("a.py", "a\n", "b")


def test_run_store_round_trip_history_and_missing_run(prepared):
    repo, record, store = prepared
    store.save(record)
    second = record_for(repo, "run_2")
    second.plan.created_at = "2026-02-01T00:00:00+00:00"
    second.status = RunStatus.APPLIED
    store.save(second)

    loaded = store.get("run_1")
    assert loaded.plan.changes[0].original_hash == sha256_text(ORIGINAL)
    assert [r.run_id for r in store.list_runs()] == ["run_2", "run_1"]
    assert store.latest_id() == "run_2"
    with pytest.raises(RunNotFoundError):
        store.get("run_missing")
    # Reopening the database keeps history (and the schema migration is idempotent).
    assert RunStore(store.home).get("run_2").status is RunStatus.APPLIED


def test_history_does_not_store_credentials_or_full_sources(prepared, monkeypatch):
    repo, record, store = prepared
    monkeypatch.setenv("GROQ_API_KEY", "gsk_must_never_be_persisted")
    store.save(record)
    raw = (store.home / "codeshift.db").read_bytes()
    assert b"gsk_must_never_be_persisted" not in raw


def test_apply_then_rollback_restores_the_original(prepared):
    repo, record, store = prepared
    apply_changes(repo, record.plan.changes, store.artifacts, "run_1")
    assert (repo / "a.py").read_text() == PROPOSED
    assert record.plan.changes[0].status is ChangeStatus.APPLIED
    rollback_changes(repo, record.plan.changes, store.artifacts, "run_1")
    assert (repo / "a.py").read_text() == ORIGINAL
    assert record.plan.changes[0].status is ChangeStatus.ROLLED_BACK


def test_apply_refuses_when_the_file_changed_after_planning(prepared):
    repo, record, store = prepared
    (repo / "a.py").write_text("x = 1  # my local edit\n")
    with pytest.raises(StaleSourceError, match="a.py"):
        apply_changes(repo, record.plan.changes, store.artifacts, "run_1")
    assert (repo / "a.py").read_text() == "x = 1  # my local edit\n"


def test_rollback_refuses_to_overwrite_later_edits(prepared):
    repo, record, store = prepared
    apply_changes(repo, record.plan.changes, store.artifacts, "run_1")
    (repo / "a.py").write_text(PROPOSED + "y = 3  # written after the migration\n")
    with pytest.raises(StaleSourceError):
        rollback_changes(repo, record.plan.changes, store.artifacts, "run_1")
    assert "written after the migration" in (repo / "a.py").read_text()


def test_apply_is_all_or_nothing_across_files(prepared):
    repo, record, store = prepared
    (repo / "b.py").write_text("changed by someone else\n")
    record.plan.changes.append(change_for("b.py"))
    store.artifacts.write("run_1", "proposed", "b.py", PROPOSED.encode())
    with pytest.raises(StaleSourceError):
        apply_changes(repo, record.plan.changes, store.artifacts, "run_1")
    assert (repo / "a.py").read_text() == ORIGINAL


def test_apply_fails_clearly_on_read_only_files(prepared):
    repo, record, store = prepared
    (repo / "a.py").chmod(0o444)
    repo.chmod(0o555)
    try:
        with pytest.raises(ValidationError, match="read-only"):
            apply_changes(repo, record.plan.changes, store.artifacts, "run_1")
    finally:
        repo.chmod(0o755)
    assert (repo / "a.py").read_text() == ORIGINAL


def test_paths_cannot_escape_repository_or_artifact_store(prepared, tmp_path):
    repo, record, store = prepared
    record.plan.changes[0].file = "../outside.py"
    with pytest.raises(ValidationError, match="escapes"):
        apply_changes(repo, record.plan.changes, store.artifacts, "run_1")
    with pytest.raises(ValidationError, match="escapes"):
        ArtifactStore(tmp_path / "runs").write("run_1", "proposed", "../../evil.py", b"x")


def test_dirty_files_reports_uncommitted_changes(tmp_path: Path):
    assert dirty_files(tmp_path) is None  # not a repository
    git = ["git", "-C", str(tmp_path), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], check=True)
    (tmp_path / "a.py").write_text("x = 1\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    assert dirty_files(tmp_path) == []
    (tmp_path / "a.py").write_text("x = 2\n")
    assert dirty_files(tmp_path) == ["a.py"]


def stage(name: str, status: StageStatus, mandatory: bool = True) -> StageResult:
    return StageResult(name=name, status=status, mandatory=mandatory)


def test_aggregation_never_treats_unexecuted_checks_as_passing():
    ok = [stage("syntax", StageStatus.PASSED), stage("structural_checks", StageStatus.PASSED)]
    assert aggregate(ok) is OverallStatus.PASSED
    assert aggregate(ok[:1]) is OverallStatus.INCOMPLETE
    assert aggregate([*ok, stage("unit_tests", StageStatus.NOT_RUN)]) is OverallStatus.INCOMPLETE
    assert aggregate([*ok, stage("unit_tests", StageStatus.FAILED)]) is OverallStatus.FAILED
    assert (
        aggregate([*ok, stage("unit_tests", StageStatus.FAILED), stage("x", StageStatus.NOT_RUN)])
        is OverallStatus.FAILED
    )
    assert (
        aggregate([*ok, stage("semantic_review", StageStatus.REVIEW_REQUIRED)])
        is OverallStatus.REVIEW_REQUIRED
    )
    # Informational stages (pre-existing failures, optional tests) do not decide the outcome.
    assert (
        aggregate([*ok, stage("unit_tests", StageStatus.FAILED, mandatory=False)])
        is OverallStatus.PASSED
    )
    assert aggregate([*ok, stage("static_analysis", StageStatus.SKIPPED)]) is OverallStatus.PASSED


def test_syntax_stage_reports_errors_and_target_version():
    assert verify_syntax({"a.py": "x = 1\n"}).status is StageStatus.PASSED
    broken = verify_syntax({"a.py": "def f(:\n"})
    assert broken.status is StageStatus.FAILED and "a.py" in broken.details[0]
    too_new = verify_syntax({"a.py": "match x:\n    case 1:\n        pass\n"}, "3.9")
    assert too_new.status is StageStatus.FAILED
    assert "not valid Python 3.9" in too_new.details[0]


def test_junit_parsing_separates_passed_failed_and_skipped(tmp_path: Path):
    report = tmp_path / "junit.xml"
    report.write_text(
        "<testsuites><testsuite>"
        '<testcase classname="t.a" name="ok"/>'
        '<testcase classname="t.a" name="bad"><failure message="x"/></testcase>'
        '<testcase classname="t.a" name="boom"><error message="x"/></testcase>'
        '<testcase classname="t.a" name="later"><skipped/></testcase>'
        "</testsuite></testsuites>"
    )
    assert _parse_junit(report) == (1, {"t.a::bad", "t.a::boom"})
    assert _parse_junit(tmp_path / "missing.xml") is None
