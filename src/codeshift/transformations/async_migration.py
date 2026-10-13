"""Repository-wide sync-to-async migration.

The engine is deliberately conservative. Functions that perform supported
blocking I/O are grouped with every in-repository caller that would have to
become a coroutine; a group is converted only if every member and every call
site can be handled, otherwise the whole group is reported as a proposal.
"""

from __future__ import annotations

import ast
import logging
import textwrap
from dataclasses import dataclass, field

import libcst as cst
from libcst.metadata import MetadataWrapper, ParentNodeProvider, PositionProvider

from codeshift.analysis.ast_analyzer import ModuleInfo, parse_module
from codeshift.analysis.callgraph import (
    ROUTE_DECORATOR_ATTRS,
    CallGraph,
    CallSite,
    FunctionInfo,
)
from codeshift.analysis.cst_analyzer import CountingTransformer, manage_imports, parse_cst
from codeshift.discovery.manifest import sha256_text
from codeshift.discovery.repository import is_test_path
from codeshift.exceptions import LLMError, TransformError
from codeshift.llm.schemas import MigrationRequest
from codeshift.models.migrations import ChangeLocation, ConversionGroup, Proposal
from codeshift.transformations.base import syntax_error
from codeshift.transformations.llm_transformer import LLMTransformer, ValidatedReplacement

log = logging.getLogger("codeshift.async")

RULE_CONVERT = "async.sync-io-candidate"
RULE_IN_ASYNC = "async.blocking-call-in-async"
HARMLESS_DECORATORS = frozenset({"staticmethod", "classmethod"})
RELEVANT_IMPORT_ROOTS = ("requests", "time", "httpx", "asyncio")


@dataclass
class AsyncMigrationResult:
    sources: dict[str, str] = field(default_factory=dict)
    groups: list[ConversionGroup] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)
    converted: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    llm_files: set[str] = field(default_factory=set)
    rules_by_file: dict[str, set[str]] = field(default_factory=dict)
    locations: dict[str, list[ChangeLocation]] = field(default_factory=dict)


@dataclass
class _Group:
    seeds: list[str]
    members: set[str]
    sites: list[CallSite] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    replacements: list[tuple[str, ValidatedReplacement]] = field(default_factory=list)
    used_llm: bool = False


Site = tuple[int, int]


@dataclass
class _FilePlan:
    make_async: set[str] = field(default_factory=set)
    awaits: set[Site] = field(default_factory=set)
    runs: set[Site] = field(default_factory=set)
    sleeps: set[Site] = field(default_factory=set)


# -- CST rewrites ------------------------------------------------------------


class _QualnameMixin(cst.CSTTransformer):
    """Tracks the dotted name of the definition being visited."""

    def __init__(self) -> None:
        super().__init__()
        self._stack: list[str] = []

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        self._stack.append(node.name.value)
        return True

    def leave_ClassDef(
        self, original_node: cst.ClassDef, updated_node: cst.ClassDef
    ) -> cst.BaseStatement:
        self._stack.pop()
        return updated_node

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        self._stack.append(node.name.value)
        return True

    def _leave_function(self) -> str:
        qualname = ".".join(self._stack)
        self._stack.pop()
        return qualname


class _FunctionSplicer(_QualnameMixin):
    """Swap one function's body for a validated replacement, keeping its original signature."""

    def __init__(self, qualname: str, replacement: cst.FunctionDef) -> None:
        super().__init__()
        self.qualname = qualname
        self.replacement = replacement
        self.count = 0

    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.BaseStatement:
        if self._leave_function() != self.qualname:
            return updated_node
        self.count += 1
        return updated_node.with_changes(
            asynchronous=self.replacement.asynchronous or cst.Asynchronous(),
            body=self.replacement.body,
        )


def splice_function(source: str, qualname: str, replacement: ValidatedReplacement) -> str:
    """Replace the body of `qualname` in `source`.

    Name, parameters, decorators and annotations come from the original, so an
    accepted replacement cannot change the function's interface.
    """
    parsed = parse_cst(replacement.replacement_code)
    functions = [stmt for stmt in parsed.body if isinstance(stmt, cst.FunctionDef)]
    if len(functions) != 1:
        raise TransformError(f"replacement for {qualname} is not a single function")
    splicer = _FunctionSplicer(qualname, functions[0])
    updated = parse_cst(source).visit(splicer)
    if splicer.count != 1:
        raise TransformError(
            f"expected exactly one definition of {qualname}, found {splicer.count}"
        )
    unused = {("requests", None), ("time", None), ("time", "sleep")}
    return manage_imports(updated, replacement.required_imports, unused).code


class _Propagator(CountingTransformer, _QualnameMixin):
    """Make functions async and rewrite the call sites that must await them."""

    METADATA_DEPENDENCIES = (PositionProvider, ParentNodeProvider)

    def __init__(
        self,
        make_async: set[str],
        await_sites: set[tuple[int, int]],
        run_sites: set[tuple[int, int]],
        sleep_sites: set[tuple[int, int]],
    ) -> None:
        CountingTransformer.__init__(self)
        self._stack = []
        self.make_async = make_async
        self.await_sites = await_sites
        self.run_sites = run_sites
        self.sleep_sites = sleep_sites
        self.done_async: set[str] = set()
        self.done_sites: set[tuple[int, int]] = set()

    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.BaseStatement:
        qualname = self._leave_function()
        if qualname in self.make_async and updated_node.asynchronous is None:
            self.count += 1
            self.done_async.add(qualname)
            return updated_node.with_changes(asynchronous=cst.Asynchronous())
        return updated_node

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.BaseExpression:
        start = self.get_metadata(PositionProvider, original_node.func).start
        key = (start.line, start.column)
        if key in self.sleep_sites:
            updated_node = updated_node.with_changes(
                func=cst.Attribute(value=cst.Name("asyncio"), attr=cst.Name("sleep"))
            )
            self.add_imports.add(("asyncio", None))
            self.remove_imports.update({("time", None), ("time", "sleep")})
        elif key in self.run_sites:
            self.count += 1
            self.done_sites.add(key)
            self.add_imports.add(("asyncio", None))
            return cst.Call(
                func=cst.Attribute(value=cst.Name("asyncio"), attr=cst.Name("run")),
                args=[cst.Arg(value=updated_node.with_changes(lpar=[], rpar=[]))],
                lpar=updated_node.lpar,
                rpar=updated_node.rpar,
            )
        elif key not in self.await_sites:
            return updated_node
        self.count += 1
        self.done_sites.add(key)
        parent = self.get_metadata(ParentNodeProvider, original_node, None)
        needs_parens = (
            isinstance(parent, (cst.Attribute, cst.Subscript)) and parent.value is original_node
        ) or (isinstance(parent, cst.Call) and parent.func is original_node)
        if needs_parens and not updated_node.lpar:
            return cst.Await(
                expression=updated_node, lpar=[cst.LeftParen()], rpar=[cst.RightParen()]
            )
        return cst.Await(expression=updated_node)


def propagate(
    module: ModuleInfo,
    make_async: set[str],
    await_sites: set[tuple[int, int]],
    run_sites: set[tuple[int, int]],
    sleep_sites: set[tuple[int, int]],
) -> str:
    """Apply async propagation to one module and confirm every planned edit happened."""
    needs_asyncio = bool(run_sites or sleep_sites)
    if needs_asyncio and (
        module.imports.get("asyncio", "asyncio") != "asyncio" or "asyncio" in module.rebinds
    ):
        raise TransformError(f"{module.path}: the name 'asyncio' is bound to something else")
    transformer = _Propagator(make_async, await_sites, run_sites, sleep_sites)
    updated = MetadataWrapper(parse_cst(module.source)).visit(transformer)
    missing_defs = make_async - transformer.done_async
    missing_sites = (await_sites | run_sites | sleep_sites) - transformer.done_sites
    if missing_defs or missing_sites:
        raise TransformError(
            f"{module.path}: could not locate {len(missing_defs)} definition(s) and "
            f"{len(missing_sites)} call site(s) planned for conversion"
        )
    return manage_imports(updated, transformer.add_imports, transformer.remove_imports).code


# -- planning ----------------------------------------------------------------


def _decorator_allowed(info: FunctionInfo, decorator: str) -> bool:
    if decorator in HARMLESS_DECORATORS:
        return True
    return info.module_uses_async_framework and decorator.split(".")[-1] in ROUTE_DECORATOR_ATTRS


def function_blockers(graph: CallGraph, info: FunctionInfo) -> list[str]:
    """Reasons a synchronous function cannot safely be turned into a coroutine."""
    reasons = []
    name = info.qualname
    if info.nested:
        reasons.append(f"{name}: nested function, its callers cannot be tracked")
    if info.is_generator:
        reasons.append(f"{name}: generator function, async generators have different semantics")
    if info.name.startswith("__") and info.name.endswith("__"):
        reasons.append(f"{name}: special method cannot be a coroutine")
    for decorator in info.decorators:
        if not _decorator_allowed(info, decorator):
            reasons.append(f"{name}: decorator @{decorator} may not support coroutine functions")
    if info.class_has_bases:
        reasons.append(f"{name}: method of a class with base classes, overrides cannot be checked")
    if graph.ambiguous_method_name(info):
        reasons.append(f"{name}: several classes define a method with this name")
    refs = graph.value_refs.get(info.id, [])
    if refs:
        reasons.append(f"{name}: passed around as a value at {', '.join(refs[:3])}")
    for call in info.unsupported_blocking:
        reasons.append(
            f"{name}: also calls {call.qualified}() at line {call.line}, "
            "which has no supported async equivalent"
        )
    return reasons


def site_blocker(graph: CallGraph, site: CallSite) -> str | None:
    callee = graph.functions[site.callee].qualname
    if site.in_lambda:
        return f"{callee} is called inside a lambda at {site.file}:{site.line}"
    if site.in_genexp:
        return f"{callee} is called inside a generator expression at {site.file}:{site.line}"
    if site.caller is None:
        return f"{callee} is called at module level at {site.file}:{site.line}"
    return None


def build_groups(graph: CallGraph) -> list[_Group]:
    """Group blocking functions with the callers that must change alongside them."""
    groups: list[_Group] = []
    seeds = [
        info for info in graph.functions.values() if info.supported_blocking and not info.in_test
    ]
    for seed in sorted(seeds, key=lambda i: (i.file, i.line)):
        members = {seed.id}
        sites: list[CallSite] = []
        queue = [] if seed.is_async else [seed.id]
        while queue:
            current = queue.pop()
            for site in graph.callers_of(current):
                sites.append(site)
                if site.in_test or site.caller is None:
                    continue
                caller = graph.functions[site.caller]
                if not caller.is_async and caller.id not in members:
                    members.add(caller.id)
                    queue.append(caller.id)
        merged = _Group(seeds=[seed.id], members=members, sites=sites)
        remaining = []
        for group in groups:
            if group.members & merged.members:
                merged.seeds = [*group.seeds, *merged.seeds]
                merged.members |= group.members
                merged.sites = [*group.sites, *[s for s in merged.sites if s not in group.sites]]
            else:
                remaining.append(group)
        groups = [*remaining, merged]

    for group in groups:
        for member in sorted(group.members):
            info = graph.functions[member]
            if not info.is_async:
                group.blockers.extend(function_blockers(graph, info))
                if not graph.callers_of(member) and not info.decorators:
                    group.warnings.append(
                        f"{info.qualname} has no call sites in this repository; "
                        "external callers must be updated to await it"
                    )
        for site in group.sites:
            blocker = site_blocker(graph, site)
            if blocker:
                group.blockers.append(blocker)
            elif site.heuristic:
                group.warnings.append(
                    f"call at {site.file}:{site.line} was matched by method name only"
                )
    return groups


# -- execution ---------------------------------------------------------------


class AsyncMigrator:
    """Plans and performs the async migration over in-memory sources."""

    def __init__(
        self,
        modules: dict[str, ModuleInfo],
        transformer: LLMTransformer | None = None,
        fallback: LLMTransformer | None = None,
        provider_note: str | None = None,
    ) -> None:
        self.modules = modules
        self.transformer = transformer
        self.fallback = fallback
        self.provider_note = provider_note or (
            "requires an LLM provider (plan with --llm sample or --llm groq)"
        )

    def _request(self, graph: CallGraph, info: FunctionInfo, convert: set[str]) -> MigrationRequest:
        module = self.modules[info.file]
        node = graph.nodes[info.id]
        first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
        source = textwrap.dedent("\n".join(module.lines[first - 1 : info.end_line])) + "\n"
        aliases = {
            local: qualified
            for local, qualified in module.imports.items()
            if qualified.split(".")[0] in RELEVANT_IMPORT_ROOTS
        }
        assert module.tree is not None
        imports = [
            ast.unparse(stmt)
            for stmt in module.tree.body
            if isinstance(stmt, (ast.Import, ast.ImportFrom))
        ]
        callees = sorted(
            {
                graph.functions[site.callee].name
                for site in graph.call_sites
                if site.caller == info.id and site.callee in convert and site.callee != info.id
            }
        )
        callers = [
            f"{site.file}:{site.line}: {self.modules[site.file].lines[site.line - 1].strip()}"
            for site in graph.callers_of(info.id)[:5]
        ]
        return MigrationRequest(
            migration_id=f"mig_{sha256_text(info.id)[:10]}",
            file=info.file,
            target_symbol=info.qualname,
            objective=(
                "Convert this function to an async coroutine. Replace blocking requests calls "
                "with awaited httpx.AsyncClient calls and time.sleep with asyncio.sleep, "
                "preserving behaviour and the public signature."
            ),
            source_code=source,
            is_async=info.is_async,
            parameters=info.params,
            decorators=info.decorators,
            aliases=aliases,
            imports=imports,
            blocking_calls=[f"{c.qualified} (line {c.line})" for c in info.supported_blocking],
            callers=callers,
            async_callees=callees,
            rules=[RULE_IN_ASYNC if info.is_async else RULE_CONVERT],
            source_dependencies={"requests": "any 2.x"},
            target_dependencies={"httpx": ">=0.27"},
            constraints=[
                "Modify only the target function",
                "Keep name, parameters and decorators unchanged",
                "Only httpx and asyncio may be newly imported",
                "No placeholders or omitted code",
            ],
            verification_requirements=["syntax", "structural", "unit_tests", "semantic_review"],
        )

    def _replacements(self, graph: CallGraph, group: _Group) -> None:
        """Ask the provider for each HTTP seed. Any failure blocks the whole group."""
        for seed_id in group.seeds:
            info = graph.functions[seed_id]
            if not info.has_http:
                continue
            if self.transformer is None:
                group.blockers.append(f"{info.qualname}: {self.provider_note}")
                continue
            request = self._request(graph, info, group.members)
            try:
                replacement = self.transformer.propose(request)
            except LLMError as primary:
                if self.fallback is None:
                    group.blockers.append(f"{info.qualname}: provider failed: {primary}")
                    continue
                try:
                    replacement = self.fallback.propose(request)
                    group.warnings.append(
                        f"{info.qualname}: primary provider failed ({primary}); "
                        "used the offline sample provider instead"
                    )
                except LLMError as secondary:
                    group.blockers.append(
                        f"{info.qualname}: provider failed: {primary}; fallback failed: {secondary}"
                    )
                    continue
            group.used_llm = True
            group.replacements.append((seed_id, replacement))
            group.warnings.extend(f"{info.qualname}: {risk}" for risk in replacement.risks)
            group.warnings.extend(
                f"{info.qualname}: assumes {assumption}" for assumption in replacement.assumptions
            )

    def run(self) -> AsyncMigrationResult:
        result = AsyncMigrationResult()
        parsed = {path: module for path, module in self.modules.items() if module.ok}
        graph = CallGraph(parsed)
        groups = build_groups(graph)
        if not groups:
            return result

        for group in groups:
            if not group.blockers:
                self._replacements(graph, group)

        viable = [g for g in groups if not g.blockers]
        sources = {path: module.source for path, module in parsed.items()}
        try:
            final = self._apply(graph, viable, sources, result)
        except TransformError as exc:
            for group in viable:
                group.blockers.append(f"conversion aborted: {exc}")
            result.warnings.append(f"async migration aborted, no async changes proposed: {exc}")
            final = {}
            viable = []
            result.converted.clear()
            result.llm_files.clear()
            result.rules_by_file.clear()
            result.locations.clear()
        result.sources = final

        for index, group in enumerate(sorted(groups, key=lambda g: g.seeds), start=1):
            converted = group in viable
            labels = sorted(f"{s.label()}" for s in group.sites)
            record = ConversionGroup(
                id=f"grp_{index}",
                seeds=sorted(group.seeds),
                callers=sorted(group.members - set(group.seeds)),
                converted=converted,
                used_llm=group.used_llm and converted,
                blockers=_unique(group.blockers),
                warnings=_unique(group.warnings),
                call_sites=labels,
            )
            result.groups.append(record)
            if not converted:
                result.proposals.append(
                    Proposal(
                        id=f"prop_{index}",
                        title="Sync-to-async conversion needs manual work",
                        functions=sorted(group.members),
                        reasons=record.blockers,
                        call_sites=labels,
                    )
                )
        result.warnings.extend(_mock_warnings(parsed))
        return result

    def _apply(
        self,
        graph: CallGraph,
        viable: list[_Group],
        sources: dict[str, str],
        result: AsyncMigrationResult,
    ) -> dict[str, str]:
        if not viable:
            return {}
        convert: set[str] = set().union(*(g.members for g in viable))
        working = dict(sources)

        # Step 1: splice validated function replacements.
        for group in viable:
            for function_id, replacement in group.replacements:
                info = graph.functions[function_id]
                working[info.file] = splice_function(working[info.file], info.qualname, replacement)
                result.llm_files.add(info.file)

        # Step 2: re-analyse so positions match the spliced sources, then propagate.
        modules = {
            path: (parse_module(path, text) if text != sources[path] else self.modules[path])
            for path, text in working.items()
        }
        broken = [p for p, m in modules.items() if not m.ok]
        if broken:
            raise TransformError(f"replacement produced unparsable source in {', '.join(broken)}")
        graph2 = CallGraph(modules)
        plan: dict[str, _FilePlan] = {}

        def bucket(path: str) -> _FilePlan:
            return plan.setdefault(path, _FilePlan())

        for function_id in convert:
            if function_id not in graph2.functions:
                raise TransformError(f"{function_id} disappeared during conversion")
            info = graph2.functions[function_id]
            if not info.is_async:
                bucket(info.file).make_async.add(info.qualname)
            for call in info.supported_blocking:
                if call.kind != "sleep":
                    raise TransformError(f"{info.qualname} still contains {call.qualified}()")
                bucket(info.file).sleeps.add((call.line, call.column))
        for site in graph2.call_sites:
            if site.callee not in convert or site.awaited or site.wrapped:
                continue
            caller = graph2.functions.get(site.caller) if site.caller else None
            if caller is None or site.in_lambda or site.in_genexp:
                raise TransformError(f"call at {site.file}:{site.line} cannot await")
            if caller.id in convert or caller.is_async:
                bucket(site.file).awaits.add((site.line, site.column))
            elif site.in_test:
                bucket(site.file).runs.add((site.line, site.column))
            else:
                raise TransformError(
                    f"call at {site.file}:{site.line} is in a function that stays synchronous"
                )
        for path, edits in plan.items():
            working[path] = propagate(
                modules[path], edits.make_async, edits.awaits, edits.runs, edits.sleeps
            )

        # Step 3: independent consistency check on the final sources.
        changed = {path: text for path, text in working.items() if text != sources[path]}
        for path, text in changed.items():
            error = syntax_error(text, path)
            if error:
                raise TransformError(f"{path}: conversion produced invalid Python: {error}")
        final_modules = {
            path: (parse_module(path, text) if path in changed else self.modules[path])
            for path, text in working.items()
        }
        problems = unawaited_calls(CallGraph(final_modules), convert)
        if problems:
            raise TransformError("; ".join(problems[:3]))

        for function_id in sorted(convert):
            before = graph.functions[function_id]
            result.converted.append(function_id)
            rule = RULE_IN_ASYNC if before.is_async else RULE_CONVERT
            result.rules_by_file.setdefault(before.file, set()).add(rule)
            result.locations.setdefault(before.file, []).append(
                ChangeLocation(
                    rule_id=rule,
                    start_line=before.line,
                    end_line=before.end_line,
                    symbol=before.qualname,
                )
            )
        for path in changed:
            result.rules_by_file.setdefault(path, set()).add(RULE_CONVERT)
        return changed


def unawaited_calls(graph: CallGraph, converted: set[str]) -> list[str]:
    """Consistency problems for a set of functions that are supposed to be coroutines now."""
    problems = []
    for function_id in sorted(converted):
        info = graph.functions.get(function_id)
        if info is None:
            problems.append(f"{function_id} no longer exists")
        elif not info.is_async:
            problems.append(f"{info.qualname} in {info.file} is not async")
        elif info.supported_blocking:
            calls = ", ".join(sorted({c.qualified for c in info.supported_blocking}))
            problems.append(f"{info.qualname} in {info.file} still calls {calls}")
    for site in graph.call_sites:
        if site.callee in converted and not (site.awaited or site.wrapped):
            name = graph.functions[site.callee].qualname
            problems.append(f"{name} is called without await at {site.file}:{site.line}")
    return problems


def _mock_warnings(modules: dict[str, ModuleInfo]) -> list[str]:
    """Flag test mocks that patch `requests`, which will not intercept httpx calls."""
    warnings = []
    for path, module in sorted(modules.items()):
        if module.tree is None or not is_test_path(path):
            continue
        for node in ast.walk(module.tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and (node.value.startswith("requests.") or ".requests." in node.value)
                and len(node.value) < 120
                and " " not in node.value
            ):
                warnings.append(
                    f"{path}:{node.lineno} patches '{node.value}'; "
                    "mocks of requests do not affect httpx and need updating"
                )
    return warnings


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))
