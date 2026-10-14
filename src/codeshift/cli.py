"""CodeShift command-line interface."""

from __future__ import annotations

import functools
import json
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

import codeshift
from codeshift.analysis.detectors import count_by_rule
from codeshift.config import load_config
from codeshift.exceptions import CodeShiftError, ExitCode, ValidationError
from codeshift.logging_config import configure_logging
from codeshift.models.findings import Finding, Severity
from codeshift.models.reports import RunRecord
from codeshift.models.verification import OverallStatus
from codeshift.orchestration.migration_runner import MigrationRunner
from codeshift.reporting.report_generator import build_report, render_text
from codeshift.rules.registry import build_registry
from codeshift.storage.database import RunStore

app = typer.Typer(
    name="codeshift",
    help="CodeShift AI: analyse, migrate and verify Python code (sync-to-async and deprecated APIs).",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err = Console(stderr=True)
log = logging.getLogger("codeshift.cli")

SEVERITY_STYLE = {
    Severity.HIGH: "bold red",
    Severity.MEDIUM: "yellow",
    Severity.LOW: "cyan",
    Severity.INFO: "dim",
}

RepoArg = Annotated[Path, typer.Argument(help="Path to the repository to analyse.")]
RulesOpt = Annotated[
    str | None,
    typer.Option("--rules", help="Comma-separated pack names or rule ids. Default: all enabled."),
]
ConfigOpt = Annotated[Path | None, typer.Option("--config", help="Path to a codeshift.toml file.")]
IncludeOpt = Annotated[
    list[str] | None, typer.Option("--include", help="Extra include glob (repeatable).")
]
ExcludeOpt = Annotated[
    list[str] | None, typer.Option("--exclude", help="Extra exclude glob (repeatable).")
]
RunIdOpt = Annotated[
    str | None, typer.Option("--run-id", help="Migration run id. Default: the most recent run.")
]


def handle_errors(func: Callable[..., Any]) -> Callable[..., Any]:
    """Translate failures into stable exit codes; never report success after a failure."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except typer.Exit:
            raise
        except CodeShiftError as exc:
            err.print(f"[bold red]error:[/bold red] {exc}", highlight=False)
            raise typer.Exit(int(exc.exit_code)) from exc
        except KeyboardInterrupt:
            err.print("[bold red]interrupted[/bold red]")
            raise typer.Exit(int(ExitCode.INTERNAL_ERROR)) from None
        except Exception as exc:  # noqa: BLE001 - last-resort boundary for the CLI
            log.exception("unexpected failure")
            err.print(
                f"[bold red]internal error:[/bold red] {type(exc).__name__}: {exc}", highlight=False
            )
            raise typer.Exit(int(ExitCode.INTERNAL_ERROR)) from exc

    return wrapper


def _version(value: bool) -> None:
    if value:
        console.print(f"codeshift {codeshift.__version__}")
        raise typer.Exit()


@app.callback()
def main(
    log_level: Annotated[
        str, typer.Option("--log-level", help="DEBUG, INFO, WARNING or ERROR.")
    ] = "WARNING",
    json_logs: Annotated[
        bool, typer.Option("--json-logs", help="Emit structured JSON logs on stderr.")
    ] = False,
    version: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="Show the version.")
    ] = False,
) -> None:
    configure_logging(log_level, json_logs)


def _selection(rules: str | None) -> list[str] | None:
    return [token.strip() for token in rules.split(",") if token.strip()] if rules else None


def _runner(repo: Path, config: Path | None = None, with_store: bool = True) -> MigrationRunner:
    resolved = repo.expanduser().resolve()
    if not resolved.is_dir():
        raise ValidationError(f"repository path is not a directory: {repo}")
    runner = MigrationRunner(
        resolved, load_config(resolved, config), RunStore() if with_store else None
    )
    return runner


def _run_context(run_id: str | None) -> tuple[MigrationRunner, RunRecord]:
    store = RunStore()
    resolved = run_id or store.latest_id()
    if resolved is None:
        raise ValidationError("no migration runs recorded yet; create one with `codeshift plan`")
    record = store.get(resolved)
    if run_id is None:
        err.print(f"[dim]using most recent run {resolved}[/dim]")
    repo = Path(record.plan.repo)
    if not repo.is_dir():
        raise ValidationError(f"repository for run {resolved} no longer exists: {repo}")
    return MigrationRunner(repo, load_config(repo), store), record


def _findings_table(findings: list[Finding], limit: int) -> Table:
    table = Table(show_lines=False, header_style="bold")
    for column in ("Severity", "Rule", "Location", "Fix", "Description"):
        table.add_column(column, overflow="fold")
    for finding in findings[:limit]:
        style = SEVERITY_STYLE[finding.severity]
        table.add_row(
            f"[{style}]{finding.severity.value.upper()}[/{style}]",
            finding.rule_id,
            f"{finding.file}:{finding.start_line}",
            finding.transform_kind.value,
            finding.description,
        )
    return table


def _pack_counts(runner: MigrationRunner, findings: list[Finding]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        pack = runner.registry.get(finding.rule_id).spec.pack
        counts[pack] = counts.get(pack, 0) + 1
    return counts


# -- read-only commands ------------------------------------------------------


@app.command()
@handle_errors
def scan(
    repo: RepoArg,
    config: ConfigOpt = None,
    include: IncludeOpt = None,
    exclude: ExcludeOpt = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the manifest as JSON.")] = False,
) -> None:
    """Scan a repository and summarise what could be migrated. Modifies nothing."""
    runner = _runner(repo, config, with_store=False)
    with err.status("Scanning repository..."):
        result = runner.analyze(include=include, exclude=exclude, use_cache=False)
    manifest = result.manifest
    packs = _pack_counts(runner, result.findings)
    if as_json:
        payload = manifest.model_dump(mode="json")
        payload["potential_migrations"] = packs
        console.print_json(json.dumps(payload))
        return
    console.print("[bold]CodeShift AI - Repository Analysis[/bold]\n")
    console.print(f"Repository: {manifest.root}", soft_wrap=True)
    console.print(f"Git repository: {'yes' if manifest.is_git_repo else 'no'}")
    console.print(f"Python files discovered: {len(manifest.files)}")
    console.print(f"Files parsed successfully: {manifest.parsed_count}")
    console.print(f"Files skipped: {len(manifest.skipped)}")
    console.print(f"Parse failures: {len(manifest.parse_failures)}")
    if manifest.python_requires:
        console.print(f"Declared Python requirement: {manifest.python_requires}")
    console.print(f"Target Python: {manifest.target_python or 'not set'}")
    if manifest.test_dirs:
        console.print(f"Test directories: {', '.join(manifest.test_dirs)}")
    if manifest.config_files:
        console.print(f"Configuration files: {', '.join(manifest.config_files)}")
    for failure in manifest.parse_failures:
        console.print(
            f"  [red]parse failure[/red] {failure.path}: {failure.reason}", highlight=False
        )
    console.print("\nPotential migrations:")
    labels = {
        "deprecated-api": "Deprecated API patterns",
        "sync-to-async": "Blocking I/O candidates",
        "python-modernization": "Legacy syntax patterns",
        "custom": "Project rule matches",
    }
    for pack, label in labels.items():
        if pack in packs or pack != "custom":
            console.print(f"  {label}: {packs.get(pack, 0)}")
    console.print(f"\nScan time: {result.timings.total_s}s")
    console.print("\nNo source files modified.")


@app.command()
@handle_errors
def analyze(
    repo: RepoArg,
    rules: RulesOpt = None,
    config: ConfigOpt = None,
    include: IncludeOpt = None,
    exclude: ExcludeOpt = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print findings as JSON.")] = False,
    limit: Annotated[int, typer.Option("--limit", help="Maximum findings to list.")] = 200,
    fail_on_findings: Annotated[
        bool,
        typer.Option("--fail-on-findings", help="Exit with code 5 when anything is found (CI)."),
    ] = False,
) -> None:
    """List migration candidates with rule, severity and location. Modifies nothing."""
    runner = _runner(repo, config)
    with err.status("Analysing..."):
        result = runner.analyze(_selection(rules), include, exclude)
    if as_json:
        console.print_json(
            json.dumps(
                {
                    "repository": result.manifest.root,
                    "files_analyzed": len(result.manifest.files),
                    "parse_failures": [f.path for f in result.manifest.parse_failures],
                    "findings_by_rule": count_by_rule(result.findings),
                    "findings": [f.model_dump(mode="json") for f in result.findings],
                }
            )
        )
    else:
        console.print(
            f"[bold]{len(result.findings)} finding(s)[/bold] in {len(result.manifest.files)} file(s) "
            f"using {len(result.rules)} rule(s)"
        )
        if result.findings:
            console.print(_findings_table(result.findings, limit))
            if len(result.findings) > limit:
                console.print(
                    f"... {len(result.findings) - limit} more (raise --limit or use --json)"
                )
        for failure in result.manifest.parse_failures:
            console.print(
                f"[red]parse failure[/red] {failure.path}: {failure.reason}", highlight=False
            )
        if result.cache_hits:
            console.print(
                f"[dim]{result.cache_hits} unchanged file(s) served from the analysis cache[/dim]"
            )
        console.print("No source files modified.")
    if fail_on_findings and result.findings:
        raise typer.Exit(int(ExitCode.FINDINGS_PRESENT))


@app.command("rules")
@handle_errors
def list_rules(
    repo: Annotated[
        Path | None, typer.Argument(help="Repository, to include its custom rules.")
    ] = None,
) -> None:
    """Show every registered migration rule."""
    custom = load_config(repo.resolve()).rules.custom if repo else []
    table = Table(header_style="bold")
    for column in ("Pack", "Rule", "Fix", "Risk", "Auto-apply safe", "Name"):
        table.add_column(column, overflow="fold")
    for rule in sorted(build_registry(custom).all(), key=lambda r: (r.spec.pack, r.spec.id)):
        spec = rule.spec
        fix = "repo-wide engine" if rule.repo_wide else spec.transform_kind.value
        table.add_row(
            spec.pack,
            spec.id,
            fix,
            spec.risk.value,
            "yes" if spec.safe_for_auto_apply else "no",
            spec.name,
        )
    console.print(table)


# -- plan / diff -------------------------------------------------------------


@app.command()
@handle_errors
def plan(
    repo: RepoArg,
    rules: RulesOpt = None,
    config: ConfigOpt = None,
    include: IncludeOpt = None,
    exclude: ExcludeOpt = None,
    llm: Annotated[
        str | None,
        typer.Option(
            "--llm", help="LLM provider for this run: off, sample, groq or openai-compatible."
        ),
    ] = None,
) -> None:
    """Create a migration plan with proposed changes. Source files are not modified."""
    if llm not in (None, "off", "sample", "groq", "openai-compatible"):
        raise ValidationError("--llm must be one of: off, sample, groq, openai-compatible")
    runner = _runner(repo, config)
    with err.status("Planning migration..."):
        record = runner.plan(_selection(rules), llm, include, exclude)
    _print_plan(record)


def _print_plan(record: RunRecord) -> None:
    plan_ = record.plan
    console.print(f"[bold]Migration plan {plan_.run_id}[/bold]")
    console.print(f"Repository: {plan_.repo}", soft_wrap=True)
    console.print(f"Files analyzed: {plan_.files_analyzed}")
    console.print(f"Findings: {len(plan_.findings)}")
    for rule_id, count in count_by_rule(plan_.findings).items():
        console.print(f"  {rule_id}: {count}")
    if plan_.llm.requests:
        console.print(
            f"LLM: {plan_.llm.provider} ({plan_.llm.model}), {plan_.llm.requests} request(s), "
            f"{plan_.llm.failed_requests} rejected or failed, {plan_.llm.redactions} redaction(s)"
        )
    console.print(f"\nProposed changes: {len(plan_.changes)} file(s)")
    for change in plan_.changes:
        flag = (
            "[yellow]review required[/yellow]"
            if change.requires_review
            else "[green]low risk[/green]"
        )
        console.print(
            f"  {change.file}  [{change.origin}]  {flag}  rules: {', '.join(change.rule_ids)}",
            highlight=False,
        )
    for group in plan_.groups:
        state = "[green]converted[/green]" if group.converted else "[yellow]proposal only[/yellow]"
        names = ", ".join(i.split("::")[1] for i in [*group.seeds, *group.callers])
        console.print(f"\nAsync group {group.id}: {state}", highlight=False)
        console.print(f"  functions: {names}", highlight=False)
        for blocker in group.blockers:
            console.print(f"  [red]blocked:[/red] {blocker}", highlight=False)
        for warning in group.warnings:
            console.print(f"  [yellow]note:[/yellow] {warning}", highlight=False)
    if plan_.warnings:
        console.print("\n[yellow]Warnings:[/yellow]")
        for warning in plan_.warnings:
            console.print(f"  - {warning}", highlight=False)
    for failure in plan_.parse_failures:
        console.print(f"[red]parse failure[/red] {failure}", highlight=False)
    console.print("\nNo source files modified.")
    if plan_.changes:
        console.print(f"Next: codeshift diff --run-id {plan_.run_id}")
    else:
        console.print("Nothing to apply.")


@app.command()
@handle_errors
def diff(run_id: RunIdOpt = None) -> None:
    """Preview the unified diff of a planned run."""
    _runner_, record = _run_context(run_id)
    if not record.plan.changes:
        console.print(f"Run {record.run_id} proposes no changes.")
        return
    text = "".join(change.diff for change in record.plan.changes)
    if console.is_terminal:
        console.print(Syntax(text, "diff", theme="ansi_dark", background_color="default"))
    else:
        sys.stdout.write(text)


# -- apply / verify / rollback ----------------------------------------------


@app.command()
@handle_errors
def apply(
    run_id: RunIdOpt = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Approve the changes without prompting.")
    ] = False,
    non_interactive: Annotated[
        bool, typer.Option("--non-interactive", help="Never prompt; fail if approval is missing.")
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Run the safety checks only.")] = False,
) -> None:
    """Apply a planned run to the working tree after explicit approval."""
    runner, record = _run_context(run_id)
    risky = runner.needs_review(record)
    console.print(
        f"Run {record.run_id}: {len(record.plan.changes)} file(s) to modify in {record.plan.repo}"
    )
    for change in record.plan.changes:
        marker = "review required" if change.requires_review else "low risk"
        console.print(f"  {change.file} ({marker})", highlight=False)
    if dry_run:
        from codeshift.git.rollback import _check_hashes, _check_writable

        _check_hashes(runner.repo, {c.file: c.original_hash for c in record.plan.changes}, "apply")
        _check_writable(runner.repo, [c.file for c in record.plan.changes])
        console.print("Dry run: all files match the plan and are writable. Nothing was written.")
        return
    approved = yes
    if not approved:
        interactive = not non_interactive and sys.stdin.isatty()
        if interactive:
            question = (
                f"{len(risky)} change(s) need review. Have you reviewed the diff and want to apply?"
                if risky
                else "Apply these changes?"
            )
            approved = typer.confirm(question, default=False)
            if not approved:
                raise ValidationError("not applied: approval was declined")
        elif risky or not runner.config.migrations.auto_apply_low_risk:
            raise ValidationError(
                "not applied: approval required. Review the diff and pass --yes "
                "(non-interactive mode never prompts)."
            )
    outcome = runner.apply(record.run_id, approved=approved)
    for warning in outcome.warnings:
        console.print(f"[yellow]warning:[/yellow] {warning}", highlight=False)
    console.print(f"[green]Applied[/green] {len(outcome.record.plan.changes)} file(s).")
    console.print(f"Next: codeshift verify --run-id {record.run_id} --run-tests")


@app.command()
@handle_errors
def verify(
    run_id: RunIdOpt = None,
    run_tests: Annotated[
        bool | None,
        typer.Option(
            "--run-tests/--no-run-tests", help="Execute the project's tests in a temporary copy."
        ),
    ] = None,
) -> None:
    """Verify a planned or applied run: syntax, structure, static analysis, tests."""
    runner, record = _run_context(run_id)
    with err.status("Verifying..."):
        record = runner.verify(record.run_id, run_tests)
    console.print(render_text(record), highlight=False)
    assert record.verification is not None
    if record.verification.overall_status is not OverallStatus.PASSED:
        raise typer.Exit(int(ExitCode.VERIFICATION_FAILED))


@app.command()
@handle_errors
def rollback(run_id: RunIdOpt = None) -> None:
    """Restore the files changed by a run, unless they were edited afterwards."""
    runner, record = _run_context(run_id)
    record = runner.rollback(record.run_id)
    console.print(
        f"[green]Rolled back[/green] run {record.run_id}: {len(record.plan.changes)} file(s) restored."
    )


# -- history / report --------------------------------------------------------


@app.command()
@handle_errors
def history(
    limit: Annotated[int, typer.Option("--limit", help="Number of runs to show.")] = 20,
) -> None:
    """Show previous migration runs."""
    records = RunStore().list_runs(limit)
    if not records:
        console.print("No migration runs recorded.")
        return
    if console.width < 100:
        # Narrow terminals: one block per run instead of a squashed table.
        for record in records:
            verification = record.verification.overall_status.value if record.verification else "-"
            console.print(f"[bold]{record.run_id}[/bold]", highlight=False)
            console.print(
                f"  {record.plan.created_at}  status={record.status.value}  "
                f"files={len(record.plan.changes)}  verification={verification}",
                highlight=False,
            )
            console.print(f"  {record.plan.repo}", highlight=False, soft_wrap=True)
        return
    table = Table(header_style="bold")
    table.add_column("Run ID", no_wrap=True)
    for column in ("Created (UTC)", "Status", "Files", "Verification", "Repository"):
        table.add_column(column, overflow="fold")
    for record in records:
        verification = record.verification.overall_status.value if record.verification else "-"
        table.add_row(
            record.run_id,
            record.plan.created_at,
            record.status.value,
            str(len(record.plan.changes)),
            verification,
            record.plan.repo,
        )
    console.print(table)


@app.command()
@handle_errors
def report(
    run_id: RunIdOpt = None,
    fmt: Annotated[str, typer.Option("--format", help="text or json.")] = "text",
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write the report to a file.")
    ] = None,
) -> None:
    """Print or export the report of a run."""
    if fmt not in ("text", "json"):
        raise ValidationError("--format must be 'text' or 'json'")
    _runner_, record = _run_context(run_id)
    text = build_report(record).model_dump_json(indent=2) if fmt == "json" else render_text(record)
    if output is not None:
        try:
            output.write_text(text + "\n", "utf-8")
        except OSError as exc:
            raise ValidationError(
                f"cannot write report to {output}: {exc.strerror or exc}"
            ) from exc
        err.print(f"Report written to {output}")
    else:
        sys.stdout.write(text + "\n")


@app.command()
@handle_errors
def purge(
    run_id: Annotated[str, typer.Option("--run-id", help="Run whose stored artifacts to delete.")],
) -> None:
    """Delete the stored proposed files and backups of a run (history is kept)."""
    store = RunStore()
    store.get(run_id)
    removed = store.artifacts.purge(run_id)
    console.print(f"Artifacts for {run_id} {'deleted' if removed else 'were already absent'}.")


@app.command()
@handle_errors
def serve(
    host: Annotated[str, typer.Option("--host", help="Interface to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port to listen on.")] = 8000,
) -> None:
    """Start the REST API (FastAPI)."""
    import uvicorn

    uvicorn.run("codeshift.api.app:app", host=host, port=port, log_level="info")
