"""REST API over the same engine the CLI uses.

Repositories are addressed by a path relative to `CODESHIFT_WORKSPACE_ROOT`
(default: the server's working directory), so a request can never point the
engine at an arbitrary location on the host. Set `CODESHIFT_API_TOKEN` to
require a bearer token.
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

import codeshift
from codeshift.analysis.ast_analyzer import parse_module
from codeshift.analysis.detectors import count_by_rule, run_detectors
from codeshift.config import RulesConfig, load_config
from codeshift.exceptions import CodeShiftError, ExitCode, RunNotFoundError
from codeshift.git.diff import unified_diff
from codeshift.models.reports import RunRecord
from codeshift.orchestration.migration_runner import MigrationRunner
from codeshift.reporting.report_generator import build_report
from codeshift.rules.registry import AnalysisContext, build_registry
from codeshift.storage.database import RunStore
from codeshift.transformations.deterministic import apply_rules

MAX_SOURCE_CHARS = 500_000

STATUS_BY_EXIT_CODE = {
    ExitCode.VALIDATION_FAILED: 409,
    ExitCode.CONFIG_ERROR: 422,
    ExitCode.VERIFICATION_FAILED: 422,
    ExitCode.INTERNAL_ERROR: 500,
}


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RepoRequest(_Body):
    repo_path: str = Field(default=".", description="Path relative to the workspace root.")
    rules: list[str] | None = Field(default=None, description="Pack names or rule ids.")


class PlanRequest(RepoRequest):
    llm: Literal["off", "sample", "groq", "openai-compatible"] | None = None


class ApplyRequest(_Body):
    approve: bool = Field(
        default=False, description="Explicit approval, required for review-required changes."
    )


class VerifyRequest(_Body):
    run_tests: bool | None = Field(
        default=None, description="Override verification.run_tests for this call."
    )


class TransformRequest(_Body):
    source: str = Field(max_length=MAX_SOURCE_CHARS)
    filename: str = "snippet.py"
    rules: list[str] | None = None
    target_python: str | None = Field(default=None, pattern=r"^3\.\d{1,2}$")


def workspace_root() -> Path:
    return Path(os.environ.get("CODESHIFT_WORKSPACE_ROOT") or Path.cwd()).resolve()


def resolve_repo(repo_path: str) -> Path:
    root = workspace_root()
    candidate = (root / repo_path).resolve()
    if not candidate.is_relative_to(root):
        raise HTTPException(status_code=400, detail="repo_path must stay inside the workspace root")
    if not candidate.is_dir():
        raise HTTPException(status_code=404, detail=f"repository not found: {repo_path}")
    return candidate


def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
    expected = os.environ.get("CODESHIFT_API_TOKEN")
    if not expected:
        return
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


def _summary(record: RunRecord) -> dict[str, Any]:
    return build_report(record).model_dump(mode="json")


def _runner_for(run_id: str) -> tuple[MigrationRunner, RunRecord]:
    store = RunStore()
    record = store.get(run_id)
    repo = Path(record.plan.repo)
    if not repo.resolve().is_relative_to(workspace_root()):
        raise HTTPException(
            status_code=403, detail="run belongs to a repository outside the workspace"
        )
    return MigrationRunner(repo, load_config(repo), store), record


def create_app() -> FastAPI:
    api = FastAPI(
        title="CodeShift AI",
        version=codeshift.__version__,
        description="Analyse, migrate (sync-to-async, deprecated APIs) and verify Python code.",
    )
    guarded = [Depends(require_token)]

    @api.exception_handler(CodeShiftError)
    async def _codeshift_error(_request: Request, exc: CodeShiftError) -> JSONResponse:
        status = 404 if isinstance(exc, RunNotFoundError) else STATUS_BY_EXIT_CODE[exc.exit_code]
        return JSONResponse(
            status_code=status, content={"error": type(exc).__name__, "detail": str(exc)}
        )

    @api.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": codeshift.__version__}

    @api.get("/rules", dependencies=guarded)
    def rules() -> list[dict[str, Any]]:
        return [
            {**rule.spec.model_dump(mode="json"), "repo_wide": rule.repo_wide}
            for rule in build_registry().all()
        ]

    @api.post("/scan", dependencies=guarded)
    def scan(body: RepoRequest) -> dict[str, Any]:
        runner = MigrationRunner(resolve_repo(body.repo_path))
        result = runner.analyze(body.rules, use_cache=False)
        manifest = result.manifest
        return {
            "repository": body.repo_path,
            "python_files": len(manifest.files),
            "parsed": manifest.parsed_count,
            "skipped": len(manifest.skipped),
            "parse_failures": [f.path for f in manifest.parse_failures],
            "target_python": manifest.target_python,
            "test_dirs": manifest.test_dirs,
            "findings_by_rule": count_by_rule(result.findings),
            "source_files_modified": 0,
        }

    @api.post("/analyze", dependencies=guarded)
    def analyze(body: RepoRequest) -> dict[str, Any]:
        runner = MigrationRunner(resolve_repo(body.repo_path))
        result = runner.analyze(body.rules, use_cache=False)
        return {
            "repository": body.repo_path,
            "files_analyzed": len(result.manifest.files),
            "findings_by_rule": count_by_rule(result.findings),
            "findings": [f.model_dump(mode="json") for f in result.findings],
        }

    @api.post("/plan", dependencies=guarded, status_code=201)
    def plan(body: PlanRequest) -> dict[str, Any]:
        runner = MigrationRunner(resolve_repo(body.repo_path), store=RunStore())
        return _summary(runner.plan(body.rules, body.llm))

    @api.get("/runs", dependencies=guarded)
    def runs(limit: int = 20) -> list[dict[str, Any]]:
        return [
            {
                "run_id": record.run_id,
                "repo": record.plan.repo,
                "status": record.status.value,
                "created_at": record.plan.created_at,
                "files": len(record.plan.changes),
                "verification": record.verification.overall_status.value
                if record.verification
                else None,
            }
            for record in RunStore().list_runs(max(1, min(limit, 200)))
        ]

    @api.get("/runs/{run_id}", dependencies=guarded)
    def run(run_id: str) -> dict[str, Any]:
        _runner, record = _runner_for(run_id)
        return _summary(record)

    @api.get("/runs/{run_id}/diff", dependencies=guarded, response_class=PlainTextResponse)
    def diff(run_id: str) -> str:
        _runner, record = _runner_for(run_id)
        return "".join(change.diff for change in record.plan.changes)

    @api.post("/runs/{run_id}/apply", dependencies=guarded)
    def apply(run_id: str, body: ApplyRequest) -> dict[str, Any]:
        runner, _record = _runner_for(run_id)
        outcome = runner.apply(run_id, approved=body.approve)
        return {**_summary(outcome.record), "apply_warnings": outcome.warnings}

    @api.post("/runs/{run_id}/verify", dependencies=guarded)
    def verify(run_id: str, body: VerifyRequest) -> dict[str, Any]:
        runner, _record = _runner_for(run_id)
        return _summary(runner.verify(run_id, body.run_tests))

    @api.post("/runs/{run_id}/rollback", dependencies=guarded)
    def rollback(run_id: str) -> dict[str, Any]:
        runner, _record = _runner_for(run_id)
        return _summary(runner.rollback(run_id))

    @api.post("/transform", dependencies=guarded)
    def transform(body: TransformRequest) -> dict[str, Any]:
        """Stateless: run the file-local deterministic rules over one source string."""
        registry = build_registry()
        selected = [
            rule
            for rule in registry.select(body.rules, RulesConfig(), body.target_python)
            if not rule.repo_wide
        ]
        module = parse_module(body.filename, body.source)
        if not module.ok:
            raise HTTPException(status_code=422, detail=f"source does not parse: {module.error}")
        findings = run_detectors(
            {body.filename: module}, selected, AnalysisContext(target_python=body.target_python)
        )
        result = apply_rules(body.filename, body.source, selected)
        return {
            "status": result.status.value,
            "applied_rules": result.applied_rules,
            "requires_review": result.requires_review,
            "findings": [f.model_dump(mode="json") for f in findings],
            "warnings": result.warnings,
            "diff": unified_diff(body.filename, body.source, result.modified_source),
            "source": result.modified_source,
        }

    return api


app = create_app()
