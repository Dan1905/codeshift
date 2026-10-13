"""Coordinates discovery, analysis, planning, application, verification and rollback."""

from __future__ import annotations

import logging
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import codeshift
from codeshift.analysis.ast_analyzer import ModuleCache, ModuleInfo
from codeshift.analysis.callgraph import CallGraph
from codeshift.analysis.detectors import deduplicate, run_detectors
from codeshift.config import Config, load_config
from codeshift.discovery.manifest import FileStatus, Manifest, sha256_text
from codeshift.discovery.repository import DEFAULT_EXCLUDED_DIRS, discover, resolve_inside
from codeshift.exceptions import ValidationError
from codeshift.git.diff import dirty_files, unified_diff
from codeshift.git.rollback import apply_changes, file_hash, rollback_changes
from codeshift.llm.base import LLMProvider
from codeshift.llm.provider import create_provider
from codeshift.llm.sample_provider import SampleProvider
from codeshift.models.findings import Finding, TransformKind
from codeshift.models.migrations import (
    ChangeLocation,
    ChangeStatus,
    FileChange,
    LLMUsage,
    MigrationPlan,
    RunStatus,
    Timings,
)
from codeshift.models.reports import RunRecord
from codeshift.models.verification import (
    OverallStatus,
    StageResult,
    StageStatus,
    VerificationResult,
    aggregate,
)
from codeshift.rules.registry import AnalysisContext, Rule, build_registry
from codeshift.storage.database import RunStore, utc_now
from codeshift.transformations.async_migration import AsyncMigrator
from codeshift.transformations.base import syntax_error
from codeshift.transformations.deterministic import apply_rules
from codeshift.transformations.llm_transformer import LLMTransformer
from codeshift.verification.static_analysis import verify_static
from codeshift.verification.structural import semantic_review, verify_structure
from codeshift.verification.syntax import verify_syntax
from codeshift.verification.test_runner import verify_tests

log = logging.getLogger("codeshift.runner")


@dataclass
class AnalysisResult:
    manifest: Manifest
    rules: list[Rule]
    findings: list[Finding]
    modules: dict[str, ModuleInfo]
    sources: dict[str, str]
    graph: CallGraph | None = None
    timings: Timings = field(default_factory=Timings)
    cache_hits: int = 0


@dataclass
class ApplyOutcome:
    record: RunRecord
    warnings: list[str] = field(default_factory=list)


def new_run_id() -> str:
    return f"run_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"


class MigrationRunner:
    """One repository, one configuration, one history store."""

    def __init__(
        self,
        repo: Path,
        config: Config | None = None,
        store: RunStore | None = None,
        provider: LLMProvider | None = None,
    ) -> None:
        self.repo = repo.resolve()
        if not self.repo.is_dir():
            raise ValidationError(f"repository path is not a directory: {repo}")
        self.config = config or load_config(self.repo)
        self._store = store
        self._provider_override = provider
        self.registry = build_registry(self.config.rules.custom)
        self.cache = ModuleCache()

    @property
    def store(self) -> RunStore:
        if self._store is None:
            self._store = RunStore()
        return self._store

    # -- analysis (read-only) ------------------------------------------

    def scan(self, include: list[str] | None = None, exclude: list[str] | None = None) -> Manifest:
        return discover(
            self.repo,
            self.config.analysis,
            include=include,
            exclude=exclude,
            target_python=self.config.project.target_python,
        )

    def analyze(
        self,
        selection: list[str] | None = None,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        use_cache: bool = True,
    ) -> AnalysisResult:
        """Scan, parse and run detectors. Never writes to the repository."""
        total_start = time.perf_counter()
        manifest = self.scan(include, exclude)
        limit = self.config.migrations.max_files_per_run
        if len(manifest.files) > limit:
            raise ValidationError(
                f"{len(manifest.files)} Python files exceed migrations.max_files_per_run ({limit}). "
                "Narrow the scan with --include/--exclude or raise the limit."
            )
        rules = self.registry.select(selection, self.config.rules, manifest.target_python)
        repo_wide = any(rule.repo_wide for rule in rules)
        file_rules = [rule for rule in rules if not rule.repo_wide]
        rules_key = "|".join(
            [
                codeshift.__version__,
                manifest.target_python or "",
                *sorted(r.spec.id for r in file_rules),
            ]
        )

        analysis_start = time.perf_counter()
        sources: dict[str, str] = {}
        modules: dict[str, ModuleInfo] = {}
        findings: list[Finding] = []
        fresh: dict[str, list[Finding]] = {}
        cache_hits = 0
        ctx = AnalysisContext(target_python=manifest.target_python)
        for entry in manifest.files:
            try:
                source = resolve_inside(self.repo, entry.path).read_text("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                entry.status = FileStatus.PARSE_FAILED
                entry.reason = f"cannot read as UTF-8: {exc}"
                continue
            sources[entry.path] = source
            key = sha256_text(f"{rules_key}|{entry.path}|{entry.sha256}")
            cached = self._cached(key) if use_cache and not repo_wide else None
            if cached is not None:
                cache_hits += 1
                entry.status = FileStatus.PARSED
                findings.extend(cached)
                continue
            module = self.cache.get(entry.path, source)
            modules[entry.path] = module
            if not module.ok:
                entry.status = FileStatus.PARSE_FAILED
                entry.reason = module.error or "parse error"
                continue
            entry.status = FileStatus.PARSED
            local = run_detectors({entry.path: module}, file_rules, ctx)
            findings.extend(local)
            fresh[key] = local

        graph = None
        if repo_wide:
            graph = CallGraph({p: m for p, m in modules.items() if m.ok})
            graph_ctx = AnalysisContext(target_python=manifest.target_python, graph=graph)
            findings.extend(
                run_detectors(modules, [rule for rule in rules if rule.repo_wide], graph_ctx)
            )
        if use_cache and fresh and self._store is not None:
            self._store.store_findings(fresh)

        timings = Timings(
            scan_s=manifest.scan_seconds,
            analysis_s=round(time.perf_counter() - analysis_start, 4),
            total_s=round(time.perf_counter() - total_start, 4),
        )
        return AnalysisResult(
            manifest=manifest,
            rules=rules,
            findings=deduplicate(findings),
            modules=modules,
            sources=sources,
            graph=graph,
            timings=timings,
            cache_hits=cache_hits,
        )

    def _cached(self, key: str) -> list[Finding] | None:
        return self._store.cached_findings(key) if self._store is not None else None

    # -- planning ------------------------------------------------------

    def _llm(
        self, mode: str | None, usage: LLMUsage
    ) -> tuple[LLMTransformer | None, LLMTransformer | None, str | None]:
        """Resolve the provider for this run. A requested but unusable provider is an error."""
        config = self.config.llm
        if self._provider_override is not None:
            return LLMTransformer(self._provider_override, config, usage), None, None
        if mode == "off":
            return None, None, "LLM assistance was disabled for this run (--llm off)"
        if mode in ("sample", "groq", "openai-compatible"):
            config = config.model_copy(update={"enabled": True, "provider": mode})
        if not config.enabled:
            return None, None, None
        provider = create_provider(config)
        assert provider is not None
        fallback = None
        if config.fallback == "sample" and provider.name != "sample":
            fallback = LLMTransformer(SampleProvider(), config, LLMUsage())
        return LLMTransformer(provider, config, usage), fallback, None

    def plan(
        self,
        selection: list[str] | None = None,
        llm_mode: str | None = None,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
    ) -> RunRecord:
        """Analyse the repository and persist proposed changes. Source files are not modified."""
        started = time.perf_counter()
        usage = LLMUsage()
        transformer, fallback, note = self._llm(llm_mode, usage)
        analysis = self.analyze(selection, include, exclude, use_cache=False)
        modules = {p: m for p, m in analysis.modules.items() if m.ok}
        warnings: list[str] = []
        plan = MigrationPlan(
            run_id=new_run_id(),
            repo=str(self.repo),
            created_at=utc_now(),
            rules=sorted(rule.spec.id for rule in analysis.rules),
            files_analyzed=len(analysis.manifest.files),
            parse_failures=[f"{f.path}: {f.reason}" for f in analysis.manifest.parse_failures],
            findings=analysis.findings,
        )

        transform_start = time.perf_counter()
        working: dict[str, str] = {}
        rule_ids: dict[str, list[str]] = {}
        locations: dict[str, list[ChangeLocation]] = {}
        review: dict[str, bool] = {}
        origin: dict[str, str] = {}

        if any(rule.repo_wide for rule in analysis.rules):
            migration = AsyncMigrator(modules, transformer, fallback, note).run()
            plan.groups = migration.groups
            plan.proposals = migration.proposals
            plan.converted_functions = migration.converted
            warnings.extend(migration.warnings)
            for path, text in migration.sources.items():
                working[path] = text
                rule_ids[path] = sorted(migration.rules_by_file.get(path, set()))
                locations[path] = list(migration.locations.get(path, []))
                review[path] = True
                origin[path] = "llm" if path in migration.llm_files else "deterministic"

        file_rules = [rule for rule in analysis.rules if not rule.repo_wide]
        deterministic_files = sorted(
            {
                f.file
                for f in analysis.findings
                if f.transform_kind is TransformKind.DETERMINISTIC
                and not self.registry.get(f.rule_id).repo_wide
            }
        )
        for path in deterministic_files:
            current = working.get(path, analysis.sources[path])
            outcome = apply_rules(path, current, file_rules)
            warnings.extend(outcome.warnings)
            if outcome.modified_source != current:
                working[path] = outcome.modified_source
                rule_ids[path] = [*rule_ids.get(path, []), *outcome.applied_rules]
                locations.setdefault(path, []).extend(outcome.locations)
                review[path] = review.get(path, False) or outcome.requires_review
                origin.setdefault(path, "deterministic")

        for path in sorted(working):
            original = analysis.sources[path]
            proposed = working[path]
            if proposed == original:
                continue
            error = syntax_error(proposed, path)
            if error:
                warnings.append(f"{path}: combined changes do not parse and were dropped: {error}")
                continue
            if origin[path] == "llm" and self.config.migrations.require_review_for_llm_changes:
                review[path] = True
            change = FileChange(
                file=path,
                original_hash=sha256_text(original),
                proposed_hash=sha256_text(proposed),
                rule_ids=rule_ids[path],
                locations=locations.get(path, []),
                origin=origin[path],
                requires_review=review[path],
                diff=unified_diff(path, original, proposed),
            )
            self.store.artifacts.write(plan.run_id, "proposed", path, proposed.encode("utf-8"))
            plan.changes.append(change)

        dirty = dirty_files(self.repo)
        if dirty:
            touched = sorted(set(dirty) & {c.file for c in plan.changes})
            if touched:
                warnings.append(
                    "files with uncommitted changes are part of this plan: " + ", ".join(touched)
                )
        plan.warnings = list(dict.fromkeys(warnings))
        plan.llm = usage
        plan.timings = Timings(
            scan_s=analysis.timings.scan_s,
            analysis_s=analysis.timings.analysis_s,
            transform_s=round(time.perf_counter() - transform_start, 4),
            total_s=round(time.perf_counter() - started, 4),
        )
        record = RunRecord(plan=plan, updated_at=utc_now())
        self.store.save(record)
        log.info(
            "plan created",
            extra={"run_id": plan.run_id, "changes": len(plan.changes), "repo": str(self.repo)},
        )
        return record

    # -- apply / rollback ----------------------------------------------

    def needs_review(self, record: RunRecord) -> list[FileChange]:
        return [c for c in record.plan.changes if c.requires_review]

    def apply(self, run_id: str, approved: bool = False) -> ApplyOutcome:
        """Write a planned run to the working tree.

        Review-required changes need `approved=True`. Low-risk-only runs also
        need it unless `migrations.auto_apply_low_risk` is enabled.
        """
        record = self._owned(run_id)
        if record.status is not RunStatus.PLANNED:
            raise ValidationError(
                f"run {run_id} is '{record.status.value}' and cannot be applied again; "
                "create a new plan"
            )
        if not record.plan.changes:
            raise ValidationError(f"run {run_id} has no proposed changes to apply")
        risky = self.needs_review(record)
        if risky and not approved:
            raise ValidationError(
                f"{len(risky)} change(s) require review. Inspect `codeshift diff --run-id {run_id}` "
                "and approve explicitly."
            )
        if not risky and not approved and not self.config.migrations.auto_apply_low_risk:
            raise ValidationError(
                "applying needs explicit approval (or migrations.auto_apply_low_risk = true)"
            )
        warnings = []
        dirty = dirty_files(self.repo)
        if dirty is None:
            warnings.append("not a Git repository; rollback relies on CodeShift's own backups only")
        elif dirty:
            warnings.append(
                f"{len(dirty)} file(s) had uncommitted changes before applying; "
                "they are preserved, only files matching their planned hash are written"
            )
        try:
            apply_changes(self.repo, record.plan.changes, self.store.artifacts, run_id)
        except ValidationError as exc:
            record.errors.append(f"apply refused: {exc}")
            self.store.save(record)
            raise
        record.status = RunStatus.APPLIED
        record.approved_by_user = approved
        record.verification = None
        self.store.save(record)
        return ApplyOutcome(record=record, warnings=warnings)

    def rollback(self, run_id: str) -> RunRecord:
        record = self._owned(run_id)
        if not any(c.status is ChangeStatus.APPLIED for c in record.plan.changes):
            raise ValidationError(f"run {run_id} has no applied changes to roll back")
        try:
            rollback_changes(self.repo, record.plan.changes, self.store.artifacts, run_id)
        except ValidationError as exc:
            record.rollback_status = "refused"
            record.errors.append(f"rollback refused: {exc}")
            self.store.save(record)
            raise
        record.status = RunStatus.ROLLED_BACK
        record.rollback_status = "completed"
        self.store.save(record)
        return record

    def _owned(self, run_id: str) -> RunRecord:
        record = self.store.get(run_id)
        if Path(record.plan.repo).resolve() != self.repo:
            raise ValidationError(f"run {run_id} belongs to {record.plan.repo}, not {self.repo}")
        return record

    # -- verification --------------------------------------------------

    def _copy_tree(self, destination: Path) -> None:
        def ignore(_directory: str, names: list[str]) -> set[str]:
            return {n for n in names if n in DEFAULT_EXCLUDED_DIRS or n.endswith(".egg-info")}

        shutil.copytree(self.repo, destination, ignore=ignore, symlinks=True)

    def verify(self, run_id: str, run_tests: bool | None = None) -> RunRecord:
        """Run the verification pipeline for a planned or applied run.

        Tools and tests run in temporary copies: the candidate (with the
        changes) and, when enabled, a baseline (without them). The working
        tree is only read.
        """
        started = time.perf_counter()
        record = self._owned(run_id)
        plan = record.plan
        if record.status is RunStatus.ROLLED_BACK:
            raise ValidationError(f"run {run_id} was rolled back; there is nothing to verify")
        if not plan.changes:
            raise ValidationError(f"run {run_id} has no changes to verify")
        config = self.config.verification
        applied = record.status is not RunStatus.PLANNED
        tests_enabled = config.run_tests if run_tests is None else run_tests
        artifacts = self.store.artifacts

        originals: dict[str, str] = {}
        candidates: dict[str, str] = {}
        drifted: list[str] = []
        for change in plan.changes:
            path = resolve_inside(self.repo, change.file)
            on_disk = file_hash(path)
            if applied:
                originals[change.file] = artifacts.read(run_id, "backup", change.file).decode(
                    "utf-8"
                )
                if on_disk != change.proposed_hash:
                    drifted.append(change.file)
                candidates[change.file] = path.read_text("utf-8") if on_disk else ""
            else:
                if on_disk != change.original_hash:
                    drifted.append(change.file)
                originals[change.file] = path.read_text("utf-8") if on_disk else ""
                candidates[change.file] = artifacts.read(run_id, "proposed", change.file).decode(
                    "utf-8"
                )

        stages: list[StageResult] = []
        manifest = self.scan()
        syntax = verify_syntax(candidates, manifest.target_python)
        stages.append(syntax)

        all_modules: dict[str, ModuleInfo] = {}
        for entry in manifest.files:
            if entry.path in candidates:
                all_modules[entry.path] = self.cache.get(entry.path, candidates[entry.path])
                continue
            try:
                text = resolve_inside(self.repo, entry.path).read_text("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            all_modules[entry.path] = self.cache.get(entry.path, text)
        rules = [
            self.registry.get(r)
            for r in plan.rules
            if r in {x.spec.id for x in self.registry.all()}
        ]
        structure, invariants = verify_structure(
            plan, originals, candidates, all_modules, rules, drifted
        )
        stages.append(structure)

        files = sorted(candidates)
        needs_trees = syntax.status is StageStatus.PASSED and (
            config.run_ruff or config.run_mypy or tests_enabled
        )
        if needs_trees:
            with tempfile.TemporaryDirectory(prefix="codeshift-verify-") as scratch:
                candidate_tree = Path(scratch) / "candidate"
                self._copy_tree(candidate_tree)
                for rel, text in candidates.items():
                    resolve_inside(candidate_tree, rel).write_text(text, "utf-8")
                baseline_tree: Path | None = None
                if config.baseline:
                    baseline_tree = Path(scratch) / "baseline"
                    self._copy_tree(baseline_tree)
                    for rel, text in originals.items():
                        resolve_inside(baseline_tree, rel).write_text(text, "utf-8")
                static, _introduced = verify_static(
                    candidate_tree, baseline_tree, files, config.run_ruff, config.run_mypy
                )
                unit, regression = verify_tests(
                    candidate_tree, baseline_tree, manifest, config, tests_enabled
                )
        else:
            reason = (
                "skipped because syntax verification failed"
                if syntax.status is not StageStatus.PASSED
                else "disabled in configuration"
            )
            executed_nothing = syntax.status is not StageStatus.PASSED
            static = StageResult(
                name="static_analysis",
                status=StageStatus.NOT_RUN if executed_nothing else StageStatus.SKIPPED,
                details=[reason],
                mandatory=executed_nothing and (config.run_ruff or config.run_mypy),
            )
            unit, regression = verify_tests(self.repo, None, manifest, config, enabled=False)
        stages.extend([static, unit, regression])
        stages.append(
            semantic_review(
                plan,
                invariants,
                record.approved_by_user,
                structure.status is StageStatus.PASSED,
            )
        )

        overall = aggregate(stages)
        record.verification = VerificationResult(
            stages=stages,
            overall_status=overall,
            target="working_tree" if applied else "proposed_copy",
        )
        plan.timings.verification_s = round(time.perf_counter() - started, 4)
        if applied:
            if overall is OverallStatus.PASSED:
                record.status = RunStatus.VERIFIED
            elif overall is OverallStatus.FAILED:
                record.status = RunStatus.FAILED
            else:
                record.status = RunStatus.APPLIED
        self.store.save(record)
        return record


__all__ = [
    "AnalysisResult",
    "ApplyOutcome",
    "MigrationRunner",
    "new_run_id",
]
