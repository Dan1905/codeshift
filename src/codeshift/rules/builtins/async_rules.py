"""Pack `sync-to-async`: detection of blocking I/O.

These rules only detect. The rewrite is repository-wide (callers must change
together) and lives in `codeshift.transformations.async_migration`.
"""

from __future__ import annotations

from codeshift.analysis.ast_analyzer import FunctionNode, ModuleInfo
from codeshift.analysis.callgraph import FunctionInfo
from codeshift.models.findings import Finding, Severity, TransformKind
from codeshift.models.migrations import Risk, RuleSpec
from codeshift.rules.registry import AnalysisContext, Rule

PACK = "sync-to-async"
ASYNC_REFERENCES = [
    "https://docs.python.org/3/library/asyncio-dev.html#running-blocking-code",
    "https://www.python-httpx.org/async/",
    "https://www.python-httpx.org/compatibility/",
]


class _GraphRule(Rule):
    repo_wide = True

    def _functions(
        self, module: ModuleInfo, ctx: AnalysisContext
    ) -> list[tuple[FunctionInfo, FunctionNode]]:
        if ctx.graph is None:
            return []
        return [
            (info, ctx.graph.nodes[info.id])
            for info in ctx.graph.functions.values()
            if info.file == module.path and not info.in_test
        ]


class BlockingCallInAsyncRule(_GraphRule):
    spec = RuleSpec(
        id="async.blocking-call-in-async",
        name="Blocking call inside an async function",
        description="A synchronous I/O call inside `async def` blocks the whole event loop.",
        pack=PACK,
        severity=Severity.HIGH,
        risk=Risk.HIGH,
        transform_kind=TransformKind.LLM,
        requires_review=True,
        semantic_notes=(
            "time.sleep becomes `await asyncio.sleep` deterministically. requests.* calls are "
            "rewritten to httpx.AsyncClient by the configured provider; httpx differs from "
            "requests in exception types, redirect defaults and timeout defaults."
        ),
        verification=["syntax", "structural", "unit_tests", "semantic_review"],
        references=ASYNC_REFERENCES,
    )

    def detect(self, module: ModuleInfo, ctx: AnalysisContext) -> list[Finding]:
        findings = []
        for info, node in self._functions(module, ctx):
            if not info.is_async:
                continue
            for call in info.blocking:
                kind = {
                    "sleep": TransformKind.DETERMINISTIC,
                    "http": TransformKind.LLM,
                    "unsupported": TransformKind.MANUAL,
                }[call.kind]
                marker = _line_marker(node, call.line)
                findings.append(
                    self.finding(
                        module,
                        marker,
                        f"{call.qualified}() blocks the event loop inside async function "
                        f"{info.qualname}().",
                        _suggestion(call.kind),
                        kind=kind,
                        symbol=info.qualname,
                        needs_dependency_changes=call.kind == "http",
                    )
                )
        return findings


class SyncIOCandidateRule(_GraphRule):
    spec = RuleSpec(
        id="async.sync-io-candidate",
        name="Synchronous I/O function that can become async",
        description=(
            "A synchronous function performs blocking I/O that has a supported asynchronous "
            "equivalent (time.sleep, requests.*)."
        ),
        pack=PACK,
        severity=Severity.MEDIUM,
        risk=Risk.HIGH,
        transform_kind=TransformKind.LLM,
        requires_review=True,
        semantic_notes=(
            "Experimental. The function and every in-repository caller become coroutines "
            "together. Conversion is refused when a caller cannot be seen or cannot await "
            "(callbacks, lambdas, generator expressions, module-level calls, unknown decorators, "
            "generators, dunder methods, subclasses)."
        ),
        verification=["syntax", "structural", "unit_tests", "semantic_review"],
        references=ASYNC_REFERENCES,
    )

    def detect(self, module: ModuleInfo, ctx: AnalysisContext) -> list[Finding]:
        findings = []
        for info, node in self._functions(module, ctx):
            if info.is_async or not info.supported_blocking:
                continue
            calls = ", ".join(sorted({c.qualified for c in info.supported_blocking}))
            callers = len(ctx.graph.callers_of(info.id)) if ctx.graph else 0
            findings.append(
                self.finding(
                    module,
                    node,
                    f"{info.qualname}() calls {calls} synchronously; "
                    f"{callers} in-repository call site(s) found.",
                    "Convert the function to `async def` and await it from its callers.",
                    kind=TransformKind.LLM if info.has_http else TransformKind.DETERMINISTIC,
                    confidence=0.8,
                    symbol=info.qualname,
                    needs_dependency_changes=info.has_http,
                )
            )
        return findings


def _suggestion(kind: str) -> str:
    return {
        "sleep": "Use `await asyncio.sleep(...)`.",
        "http": "Use `httpx.AsyncClient` and await the request.",
        "unsupported": "Move the call to a thread (asyncio.to_thread) or use an async library.",
    }[kind]


class _Marker:
    def __init__(self, line: int) -> None:
        self.lineno = line
        self.end_lineno = line
        self.col_offset = 0


def _line_marker(node: object, line: int) -> _Marker:
    return _Marker(line)
