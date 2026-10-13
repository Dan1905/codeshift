"""Turn provider replies into validated function replacements.

A reply is accepted only after it parses as the response schema and its code
passes AST-level checks. Passing these checks means the proposal is well formed,
not that it is correct: tests and review still decide that.
"""

from __future__ import annotations

import ast
import json
import logging
import re
import textwrap
from dataclasses import dataclass, field

from pydantic import ValidationError as PydanticValidationError

from codeshift.analysis.callgraph import SUPPORTED_BLOCKING, function_params
from codeshift.config import LLMConfig
from codeshift.exceptions import LLMBudgetError, LLMError, LLMResponseError
from codeshift.llm.base import LLMProvider
from codeshift.llm.prompts import build_messages, prompt_size
from codeshift.llm.redaction import Redactor
from codeshift.llm.schemas import MigrationRequest, MigrationResponse
from codeshift.models.migrations import LLMUsage

log = logging.getLogger("codeshift.llm")

ALLOWED_IMPORT_ROOTS = frozenset({"httpx", "asyncio"})
DANGEROUS_CALLS = frozenset(
    {"eval", "exec", "compile", "__import__", "open", "input", "breakpoint"}
)
DANGEROUS_PREFIXES = (
    "os.", "subprocess.", "shutil.", "socket.", "sys.", "importlib.", "pickle.", "ctypes.",
    "builtins.", "pathlib.",
)  # fmt: skip
_FENCE = re.compile(r"^\s*```(?:json)?\s*\n(.*?)\n\s*```\s*$", re.DOTALL)


@dataclass
class ValidatedReplacement:
    """A function replacement that passed schema and AST validation."""

    file: str
    target_symbol: str
    replacement_code: str
    required_imports: set[tuple[str, str | None]] = field(default_factory=set)
    explanation: str = ""
    assumptions: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    requires_human_review: bool = True


def parse_response(text: str) -> MigrationResponse:
    """Parse provider text into the response schema, rejecting anything malformed."""
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMResponseError(f"reply is not valid JSON: {exc.msg}") from exc
    try:
        return MigrationResponse.model_validate(data)
    except PydanticValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()[:5]
        )
        raise LLMResponseError(f"reply does not match the response schema: {problems}") from exc


def _dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _called_names(tree: ast.AST) -> set[str]:
    return {
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and (name := _dotted(node.func)) is not None
    }


def _is_placeholder_body(function: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    body = [
        stmt
        for stmt in function.body
        if not (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        )
    ]
    if not body:
        return True
    for stmt in ast.walk(function):
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and stmt.value.value is Ellipsis
        ):
            return True
    return all(isinstance(stmt, ast.Pass) for stmt in body)


def validate_response(
    request: MigrationRequest, response: MigrationResponse
) -> ValidatedReplacement:
    """Check a schema-valid response against the request. Raises `LLMResponseError`."""
    if response.migration_id != request.migration_id:
        raise LLMResponseError("migration_id does not match the request")
    if len(response.changes) != 1:
        raise LLMResponseError("expected exactly one change for a single-function request")
    change = response.changes[0]
    if change.file != request.file or change.target_symbol != request.target_symbol:
        raise LLMResponseError(
            f"change targets {change.file}:{change.target_symbol}, "
            f"but only {request.file}:{request.target_symbol} may be modified"
        )

    code = textwrap.dedent(change.replacement_code).strip("\n") + "\n"
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise LLMResponseError(f"replacement_code is not valid Python: {exc.msg}") from exc
    if len(tree.body) != 1 or not isinstance(tree.body[0], (ast.FunctionDef, ast.AsyncFunctionDef)):
        raise LLMResponseError("replacement_code must contain exactly one function definition")
    function = tree.body[0]
    name = request.target_symbol.rsplit(".", 1)[-1]
    if function.name != name:
        raise LLMResponseError(f"replacement defines '{function.name}', expected '{name}'")
    if not isinstance(function, ast.AsyncFunctionDef):
        raise LLMResponseError("replacement must be declared with `async def`")
    if function_params(function) != request.parameters:
        raise LLMResponseError(
            f"replacement changes the parameters: {function_params(function)} "
            f"instead of {request.parameters}"
        )
    if _is_placeholder_body(function):
        raise LLMResponseError("replacement contains placeholder code")
    if not any(isinstance(node, ast.Await) for node in ast.walk(function)):
        raise LLMResponseError("replacement never awaits anything")

    original_tree = ast.parse(textwrap.dedent(request.source_code))
    for node in ast.walk(function):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise LLMResponseError("replacement contains an import statement in the function body")
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            raise LLMResponseError("replacement introduces global/nonlocal statements")
        if isinstance(node, ast.Name):
            qualified = request.aliases.get(node.id, "")
            if qualified == "requests" or qualified.startswith("requests."):
                raise LLMResponseError(f"replacement still references requests via '{node.id}'")
        if isinstance(node, ast.Call):
            dotted = _dotted(node.func)
            if dotted:
                head, _, rest = dotted.partition(".")
                resolved = request.aliases.get(head, head) + (f".{rest}" if rest else "")
                if resolved in SUPPORTED_BLOCKING:
                    raise LLMResponseError(f"replacement still calls blocking {resolved}()")

    introduced = _called_names(function) - _called_names(original_tree)
    for called in sorted(introduced):
        if called in DANGEROUS_CALLS or called.startswith(DANGEROUS_PREFIXES):
            raise LLMResponseError(f"replacement introduces a disallowed call: {called}()")

    imports: set[tuple[str, str | None]] = set()
    for line in change.required_imports:
        try:
            parsed = ast.parse(line.strip())
        except SyntaxError as exc:
            raise LLMResponseError(f"required import is not valid Python: {line!r}") from exc
        if len(parsed.body) != 1:
            raise LLMResponseError(f"required import must be a single statement: {line!r}")
        stmt = parsed.body[0]
        if isinstance(stmt, ast.Import):
            pairs = [(alias.name, None) for alias in stmt.names if not alias.asname]
            if len(pairs) != len(stmt.names):
                raise LLMResponseError(f"aliased imports are not supported: {line!r}")
        elif isinstance(stmt, ast.ImportFrom) and stmt.module and not stmt.level:
            pairs = [(stmt.module, alias.name) for alias in stmt.names if not alias.asname]  # type: ignore[misc]
            if len(pairs) != len(stmt.names) or any(obj == "*" for _, obj in pairs):
                raise LLMResponseError(f"aliased or star imports are not supported: {line!r}")
        else:
            raise LLMResponseError(f"required_imports entry is not an import: {line!r}")
        for module, obj in pairs:
            if module.split(".")[0] not in ALLOWED_IMPORT_ROOTS:
                raise LLMResponseError(f"import of '{module}' is not allowed")
            imports.add((module, obj))
    used = {node.id for node in ast.walk(function) if isinstance(node, ast.Name)}
    for root in ALLOWED_IMPORT_ROOTS & used:
        if root not in request.aliases:
            imports.add((root, None))

    return ValidatedReplacement(
        file=change.file,
        target_symbol=change.target_symbol,
        replacement_code=code,
        required_imports=imports,
        explanation=change.explanation,
        assumptions=list(change.assumptions),
        risks=list(response.risks),
        requires_human_review=True,
    )


class LLMTransformer:
    """Runs one validated request/response exchange with budget, redaction and retries."""

    def __init__(self, provider: LLMProvider, config: LLMConfig, usage: LLMUsage) -> None:
        self.provider = provider
        self.config = config
        self.usage = usage
        self.usage.provider = provider.name
        self.usage.model = provider.model
        self.redactor = Redactor(config.redact_patterns)

    def _check_budget(self) -> None:
        if self.usage.requests >= self.config.max_requests_per_run:
            raise LLMBudgetError(
                f"LLM request limit reached ({self.config.max_requests_per_run} per run)"
            )
        spent = self.usage.prompt_tokens + self.usage.completion_tokens
        if spent >= self.config.max_total_tokens:
            raise LLMBudgetError(
                f"LLM token limit reached ({self.config.max_total_tokens} per run)"
            )

    def _prepare(self, request: MigrationRequest) -> MigrationRequest:
        if self.provider.external:
            request = request.model_copy(
                update={
                    "source_code": self.redactor.redact(request.source_code),
                    "callers": [self.redactor.redact(c) for c in request.callers],
                    "imports": [self.redactor.redact(i) for i in request.imports],
                }
            )
            self.usage.redactions = self.redactor.count
        limit = self.config.max_context_chars
        if prompt_size(build_messages(request)) > limit:
            request = request.model_copy(update={"callers": []})
        if prompt_size(build_messages(request)) > limit:
            raise LLMError(
                f"{request.file}:{request.target_symbol} exceeds the configured context limit "
                f"({limit} characters)"
            )
        return request

    def propose(self, request: MigrationRequest) -> ValidatedReplacement:
        """Ask the provider for a replacement and validate it, with one corrective retry."""
        prepared = self._prepare(request)
        feedback: str | None = None
        last: LLMResponseError | None = None
        for _attempt in range(2):
            self._check_budget()
            self.usage.requests += 1
            try:
                reply = self.provider.generate_migration(prepared, feedback)
            except LLMError:
                self.usage.failed_requests += 1
                raise
            self.usage.prompt_tokens += reply.prompt_tokens
            self.usage.completion_tokens += reply.completion_tokens
            try:
                validated = validate_response(prepared, parse_response(reply.content))
            except LLMResponseError as exc:
                self.usage.failed_requests += 1
                last = exc
                feedback = str(exc)
                log.warning(
                    "llm reply rejected",
                    extra={
                        "provider": self.provider.name,
                        "model": self.provider.model,
                        "migration_id": request.migration_id,
                        "reason": str(exc),
                    },
                )
                continue
            validated.replacement_code = self.redactor.restore(validated.replacement_code)
            log.info(
                "llm reply accepted",
                extra={
                    "provider": self.provider.name,
                    "model": self.provider.model,
                    "migration_id": request.migration_id,
                },
            )
            return validated
        assert last is not None
        raise LLMResponseError(f"rejected after a corrective retry: {last}")
