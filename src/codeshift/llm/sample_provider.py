"""Offline, deterministic provider for development, demos and tests.

It answers the same `MigrationRequest` contract as a hosted model, using a
LibCST rewrite for a narrow, well-defined subset: functions that call
`requests.<verb>(...)` through a plain `import requests`. Anything outside that
subset is refused, so its output still goes through the normal validation path.
"""

from __future__ import annotations

import json
import textwrap

import libcst as cst
from libcst.metadata import MetadataWrapper, ParentNodeProvider

from codeshift.analysis.callgraph import HTTP_VERBS
from codeshift.exceptions import LLMResponseError
from codeshift.llm.base import LLMProvider, LLMReply
from codeshift.llm.schemas import MigrationRequest

UNSUPPORTED_KWARGS = frozenset({"verify", "cert", "proxies", "stream", "hooks"})
EXCEPTION_MAP = {
    "RequestException": "HTTPError",
    "HTTPError": "HTTPStatusError",
    "Timeout": "TimeoutException",
    "ConnectionError": "ConnectError",
    "ConnectTimeout": "ConnectTimeout",
    "ReadTimeout": "ReadTimeout",
    "TooManyRedirects": "TooManyRedirects",
}


class _Unsupported(Exception):
    pass


class _AsyncRewriter(cst.CSTTransformer):
    METADATA_DEPENDENCIES = (ParentNodeProvider,)

    def __init__(self, requests_names: set[str], sleep_exprs: set[str], client: str) -> None:
        super().__init__()
        self.requests_names = requests_names
        self.sleep_exprs = sleep_exprs
        self.client = client
        self.depth = 0
        self.http_calls = 0
        self.sleeps = 0
        self.exceptions: dict[str, str] = {}
        self.uses_ok = False

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        self.depth += 1
        return True

    def visit_Lambda(self, node: cst.Lambda) -> bool:
        self.depth += 1
        return True

    def leave_Lambda(self, original_node: cst.Lambda, updated_node: cst.Lambda) -> cst.Lambda:
        self.depth -= 1
        return updated_node

    def _await(self, original: cst.Call, call: cst.Call) -> cst.BaseExpression:
        parent = self.get_metadata(ParentNodeProvider, original, None)
        needs_parens = (
            isinstance(parent, (cst.Attribute, cst.Subscript))
            and parent.value is original
            or isinstance(parent, cst.Call)
            and parent.func is original
        )
        if needs_parens:
            return cst.Await(expression=call, lpar=[cst.LeftParen()], rpar=[cst.RightParen()])
        return cst.Await(expression=call)

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.BaseExpression:
        func = original_node.func
        if (
            isinstance(func, cst.Attribute)
            and isinstance(func.value, cst.Name)
            and func.value.value in self.requests_names
            and func.attr.value in HTTP_VERBS
        ):
            if self.depth != 1:
                raise _Unsupported("HTTP call inside a nested function or lambda")
            args = []
            for arg in updated_node.args:
                keyword = arg.keyword.value if arg.keyword else None
                if arg.star == "**":
                    raise _Unsupported("HTTP call uses **kwargs, which cannot be checked")
                if keyword in UNSUPPORTED_KWARGS:
                    raise _Unsupported(f"keyword '{keyword}' has no per-request httpx equivalent")
                if keyword == "allow_redirects":
                    arg = arg.with_changes(keyword=cst.Name("follow_redirects"))
                args.append(arg)
            self.http_calls += 1
            call = updated_node.with_changes(
                func=cst.Attribute(value=cst.Name(self.client), attr=func.attr), args=args
            )
            return self._await(original_node, call)
        if cst.Module([]).code_for_node(func) in self.sleep_exprs:
            if self.depth != 1:
                raise _Unsupported("sleep call inside a nested function or lambda")
            self.sleeps += 1
            call = updated_node.with_changes(
                func=cst.Attribute(value=cst.Name("asyncio"), attr=cst.Name("sleep"))
            )
            return self._await(original_node, call)
        return updated_node

    def leave_Attribute(
        self, original_node: cst.Attribute, updated_node: cst.Attribute
    ) -> cst.BaseExpression:
        if original_node.attr.value == "ok":
            self.uses_ok = True
        value = original_node.value
        is_requests = isinstance(value, cst.Name) and value.value in self.requests_names
        is_exceptions = (
            isinstance(value, cst.Attribute)
            and isinstance(value.value, cst.Name)
            and value.value.value in self.requests_names
            and value.attr.value == "exceptions"
        )
        if (is_requests or is_exceptions) and original_node.attr.value in EXCEPTION_MAP:
            target = EXCEPTION_MAP[original_node.attr.value]
            self.exceptions[original_node.attr.value] = target
            return cst.Attribute(value=cst.Name("httpx"), attr=cst.Name(target))
        return updated_node

    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef:
        self.depth -= 1
        if self.depth != 0:
            return updated_node
        body = updated_node.body
        if not isinstance(body, cst.IndentedBlock):
            raise _Unsupported("single-line function bodies are not supported")
        statements = list(body.body)
        head: list[cst.BaseStatement] = []
        if statements and _is_docstring(statements[0]):
            head.append(statements.pop(0))
        if self.http_calls:
            client_call = cst.parse_expression(
                "httpx.AsyncClient(follow_redirects=True, timeout=None)"
            )
            statements = [
                cst.With(
                    items=[
                        cst.WithItem(
                            item=client_call, asname=cst.AsName(name=cst.Name(self.client))
                        )
                    ],
                    body=cst.IndentedBlock(body=statements),
                    asynchronous=cst.Asynchronous(),
                )
            ]
        return updated_node.with_changes(
            body=body.with_changes(body=[*head, *statements]),
            asynchronous=updated_node.asynchronous or cst.Asynchronous(),
        )


def _is_docstring(statement: cst.BaseStatement) -> bool:
    return (
        isinstance(statement, cst.SimpleStatementLine)
        and len(statement.body) == 1
        and isinstance(statement.body[0], cst.Expr)
        and isinstance(statement.body[0].value, (cst.SimpleString, cst.ConcatenatedString))
    )


class _NameCollector(cst.CSTVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Name(self, node: cst.Name) -> None:
        self.names.add(node.value)


class SampleProvider(LLMProvider):
    """Deterministic stand-in for a hosted model. Makes no network calls."""

    name = "sample"
    model = "codeshift-sample-heuristic"
    external = False

    def generate_migration(
        self, request: MigrationRequest, feedback: str | None = None
    ) -> LLMReply:
        source = textwrap.dedent(request.source_code)
        try:
            module = cst.parse_module(source)
        except cst.ParserSyntaxError as exc:
            raise LLMResponseError(
                f"sample provider cannot parse the function: {exc.message}"
            ) from exc

        requests_names = {
            local for local, qualified in request.aliases.items() if qualified == "requests"
        }
        direct_verbs = [
            local
            for local, qualified in request.aliases.items()
            if qualified.startswith("requests.") and qualified.split(".")[1] in HTTP_VERBS
        ]
        if direct_verbs:
            raise LLMResponseError(
                "sample provider only supports `import requests` style calls, found "
                f"`from requests import {', '.join(direct_verbs)}`"
            )
        sleep_exprs = {
            f"{local}.sleep" if qualified == "time" else local
            for local, qualified in request.aliases.items()
            if qualified in ("time", "time.sleep")
        }
        collector = _NameCollector()
        module.visit(collector)
        client = next(
            name
            for name in ("client", "http_client", "_codeshift_http_client")
            if name not in collector.names
        )
        rewriter = _AsyncRewriter(requests_names, sleep_exprs, client)
        try:
            rewritten = MetadataWrapper(module).visit(rewriter)
        except _Unsupported as exc:
            raise LLMResponseError(f"sample provider cannot migrate this function: {exc}") from exc
        if not rewriter.http_calls and not rewriter.sleeps:
            raise LLMResponseError("sample provider found no supported blocking call to rewrite")

        leftovers = _NameCollector()
        rewritten.visit(leftovers)
        if leftovers.names & requests_names:
            raise LLMResponseError(
                "sample provider cannot migrate this function: it uses requests APIs outside the "
                "supported subset"
            )

        imports = []
        if rewriter.http_calls or rewriter.exceptions:
            imports.append("import httpx")
        if rewriter.sleeps:
            imports.append("import asyncio")
        assumptions = ["httpx is installed and importable in the target environment"]
        assumptions += [
            f"requests.{old} is treated as equivalent to httpx.{new}"
            for old, new in sorted(rewriter.exceptions.items())
        ]
        risks = [
            "Callers must await the function",
            "The client is created with timeout=None and follow_redirects=True to match "
            "requests defaults; consider setting an explicit timeout",
            "A new httpx.AsyncClient is opened per call, as requests.<verb>() opened a new "
            "connection per call",
        ]
        if rewriter.uses_ok:
            risks.append("httpx responses have no `.ok`; use `.is_success` if `.ok` is read here")
        payload = {
            "migration_id": request.migration_id,
            "changes": [
                {
                    "file": request.file,
                    "operation": "replace_function",
                    "target_symbol": request.target_symbol,
                    "replacement_code": rewritten.code,
                    "explanation": (
                        f"Rewrote {rewriter.http_calls} requests call(s) to an awaited "
                        f"httpx.AsyncClient and {rewriter.sleeps} sleep call(s) to asyncio.sleep."
                    ),
                    "assumptions": assumptions,
                    "verification_steps": [
                        "Run the unit tests that exercise this function",
                        "Check that every call site awaits the function",
                    ],
                    "required_imports": imports,
                }
            ],
            "risks": risks,
            "requires_human_review": True,
        }
        return LLMReply(content=json.dumps(payload), model=self.model)
