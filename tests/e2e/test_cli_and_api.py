"""End-to-end: the documented workflow through the CLI and through the REST API."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from codeshift.api.app import create_app
from codeshift.cli import app
from codeshift.exceptions import ExitCode

runner = CliRunner()


def invoke(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, list(args))
    return result.exit_code, result.output


def test_full_cli_workflow(service_repo: Path, isolated_home: Path):
    original_main = (service_repo / "app/main.py").read_text()

    code, out = invoke("scan", str(service_repo))
    assert code == 0, out
    assert "Python files discovered: 9" in out
    assert "Blocking I/O candidates: 5" in out
    assert "No source files modified." in out

    code, out = invoke("analyze", str(service_repo), "--json")
    assert code == 0, out
    findings = json.loads(out)
    assert findings["findings_by_rule"]["async.sync-io-candidate"] == 5
    code, _ = invoke("analyze", str(service_repo), "--fail-on-findings")
    assert code == ExitCode.FINDINGS_PRESENT

    code, out = invoke("plan", str(service_repo), "--llm", "sample")
    assert code == 0, out
    assert "Proposed changes: 4 file(s)" in out
    assert (service_repo / "app/main.py").read_text() == original_main

    code, out = invoke("diff")
    assert code == 0
    assert "-def stock(sku: str):" in out and "+async def stock(sku: str):" in out

    code, out = invoke("apply", "--non-interactive")
    assert code == ExitCode.VALIDATION_FAILED
    assert "approval required" in out
    assert (service_repo / "app/main.py").read_text() == original_main

    code, out = invoke("apply", "--dry-run")
    assert code == 0 and "Nothing was written" in out
    code, out = invoke("apply", "--yes")
    assert code == 0, out
    assert "async def create_order" in (service_repo / "app/main.py").read_text()
    code, out = invoke("apply", "--yes")
    assert code == ExitCode.VALIDATION_FAILED, "a run cannot be applied twice"

    code, out = invoke("verify", "--run-tests")
    assert code == 0, out
    assert "Unit tests: PASS" in out and "Overall status: PASSED" in out

    code, out = invoke("report", "--format", "json")
    assert code == 0
    report = json.loads(out[out.index("{") :])
    assert report["status"] == "verified"
    assert report["files_modified"] == 4
    assert report["verification"]["summary"]["overall_status"] == "passed"
    assert report["llm"]["provider"] == "sample"

    code, out = invoke("history")
    assert code == 0 and report["run_id"] in out

    code, out = invoke("rollback", "--run-id", report["run_id"])
    assert code == 0, out
    assert (service_repo / "app/main.py").read_text() == original_main
    code, out = invoke("rollback", "--run-id", report["run_id"])
    assert code == ExitCode.VALIDATION_FAILED


def test_cli_reports_errors_with_stable_exit_codes(tmp_path: Path, make_repo):
    assert invoke("scan", str(tmp_path / "missing"))[0] == ExitCode.VALIDATION_FAILED
    assert invoke("diff", "--run-id", "run_does_not_exist")[0] == ExitCode.VALIDATION_FAILED
    bad = make_repo({"codeshift.toml": "[llm]\nmax_retries = 99\n"})
    assert invoke("scan", str(bad))[0] == ExitCode.CONFIG_ERROR
    repo = make_repo({"a.py": "x = 1\n"}, name="plain")
    assert invoke("plan", str(repo), "--rules", "nope")[0] == ExitCode.CONFIG_ERROR
    code, out = invoke("plan", str(repo), "--llm", "groq")
    assert code == ExitCode.VALIDATION_FAILED and "GROQ_API_KEY" in out
    assert invoke("--help")[0] == 0


def test_verify_failure_exit_code(make_repo):
    repo = make_repo({"client.py": "import time\n\n\ndef wait():\n    time.sleep(1)\n"})
    assert invoke("plan", str(repo))[0] == 0
    assert invoke("apply", "--yes")[0] == 0
    code, out = invoke("verify", "--no-run-tests")
    assert code == ExitCode.VERIFICATION_FAILED
    assert "Unit tests: NOT RUN" in out and "Overall status: INCOMPLETE" in out


@pytest.fixture()
def api(service_repo: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CODESHIFT_WORKSPACE_ROOT", str(service_repo.parent))
    return TestClient(create_app())


def test_full_api_workflow(api: TestClient, service_repo: Path):
    original = (service_repo / "app/main.py").read_text()
    assert api.get("/health").json()["status"] == "ok"
    assert len(api.get("/rules").json()) == 8

    scan = api.post("/scan", json={"repo_path": "legacy_service"}).json()
    assert scan["python_files"] == 9 and scan["source_files_modified"] == 0

    plan = api.post("/plan", json={"repo_path": "legacy_service", "llm": "sample"})
    assert plan.status_code == 201, plan.text
    run_id = plan.json()["run_id"]
    assert (service_repo / "app/main.py").read_text() == original

    assert "+async def stock" in api.get(f"/runs/{run_id}/diff").text
    refused = api.post(f"/runs/{run_id}/apply", json={"approve": False})
    assert refused.status_code == 409 and "require review" in refused.json()["detail"]

    applied = api.post(f"/runs/{run_id}/apply", json={"approve": True})
    assert applied.status_code == 200 and applied.json()["status"] == "applied"
    verified = api.post(f"/runs/{run_id}/verify", json={"run_tests": True}).json()
    assert verified["verification"]["summary"]["overall_status"] == "passed"
    assert api.get("/runs").json()[0]["run_id"] == run_id

    assert api.post(f"/runs/{run_id}/rollback").json()["status"] == "rolled_back"
    assert (service_repo / "app/main.py").read_text() == original


def test_api_transform_endpoint_and_input_validation(api: TestClient):
    response = api.post(
        "/transform",
        json={"source": "import logging\nlogging.warn('x')\n", "rules": ["deprecated-api"]},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["source"] == "import logging\nlogging.warning('x')\n"
    assert body["applied_rules"] == ["python.logging.warn"] and "+logging.warning" in body["diff"]

    assert api.post("/transform", json={"source": "def (:"}).status_code == 422
    assert api.post("/scan", json={"repo_path": "../.."}).status_code == 400
    assert api.post("/scan", json={"repo_path": "nope"}).status_code == 404
    assert api.get("/runs/run_missing").status_code == 404
    assert api.post("/plan", json={"repo_path": "legacy_service", "bogus": 1}).status_code == 422


def test_api_token_is_enforced_when_configured(api: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CODESHIFT_API_TOKEN", "s3cret-token")
    assert api.get("/health").status_code == 200
    assert api.get("/rules").status_code == 401
    assert api.get("/rules", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert api.get("/rules", headers={"Authorization": "Bearer s3cret-token"}).status_code == 200
