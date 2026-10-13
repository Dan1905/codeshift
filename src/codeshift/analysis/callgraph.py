"""Repository-wide function index and call graph built from `ast`.

The graph answers the questions the sync-to-async migration depends on: which
functions perform blocking I/O, who calls them, and whether every caller is
visible and can legally contain an `await`.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from codeshift.analysis.ast_analyzer import FunctionNode, ModuleInfo
from codeshift.discovery.repository import is_test_path

HTTP_VERBS = ("get", "post", "put", "delete", "patch", "head", "options", "request")
SUPPORTED_BLOCKING: dict[str, str] = {"time.sleep": "sleep"} | {
    f"requests.{verb}": "http" for verb in HTTP_VERBS
}
# Known blocking calls for which this version has no automatic async replacement.
UNSUPPORTED_BLOCKING = frozenset(
    {
        "requests.Session", "requests.session", "urllib.request.urlopen", "subprocess.run",
        "subprocess.call", "subprocess.check_call", "subprocess.check_output", "os.system",
        "socket.create_connection", "sqlite3.connect", "http.client.HTTPConnection",
        "http.client.HTTPSConnection", "smtplib.SMTP", "ftplib.FTP",
    }
)  # fmt: skip
AWAITABLE_WRAPPERS = frozenset(
    {
        "asyncio.run", "asyncio.gather", "asyncio.create_task", "asyncio.wait_for",
        "asyncio.ensure_future", "asyncio.shield", "asyncio.timeout", "asyncio.as_completed",
    }
)  # fmt: skip
ROUTE_DECORATOR_ATTRS = frozenset(
    {"get", "post", "put", "delete", "patch", "head", "options", "api_route", "websocket"}
)
ASYNC_FRAMEWORK_ROOTS = ("fastapi", "starlette")
# Attribute names too generic to attribute to a single method by name alone.
COMMON_METHOD_NAMES = frozenset(
    {
        "get", "post", "put", "delete", "patch", "request", "json", "close", "read", "write",
        "run", "items", "keys", "values", "append", "update", "pop", "add", "send", "open",
        "start", "stop", "join", "format", "copy", "save", "load", "execute", "commit",
    }
)  # fmt: skip


@dataclass(frozen=True)
class BlockingCall:
    qualified: str
    kind: str  # "sleep", "http" or "unsupported"
    line: int
    column: int


@dataclass(frozen=True)
class CallSite:
    caller: str | None
    callee: str
    file: str
    line: int
    column: int
    awaited: bool
    wrapped: bool
    in_lambda: bool
    in_genexp: bool
    heuristic: bool
    in_test: bool

    def label(self) -> str:
        return f"{self.file}:{self.line} in {self.caller.split('::')[1] if self.caller else '<module>'}"


@dataclass
class FunctionInfo:
    id: str
    file: str
    qualname: str
    name: str
    class_name: str | None
    is_async: bool
    line: int
    end_line: int
    params: list[str]
    decorators: list[str]
    is_generator: bool
    nested: bool
    in_test: bool
    class_has_bases: bool = False
    module_uses_async_framework: bool = False
    blocking: list[BlockingCall] = field(default_factory=list)

    @property
    def supported_blocking(self) -> list[BlockingCall]:
        return [b for b in self.blocking if b.kind != "unsupported"]

    @property
    def unsupported_blocking(self) -> list[BlockingCall]:
        return [b for b in self.blocking if b.kind == "unsupported"]

    @property
    def has_http(self) -> bool:
        return any(b.kind == "http" for b in self.blocking)


def function_params(node: FunctionNode) -> list[str]:
    """Flatten a signature into comparable parameter names."""
    args = node.args
    names = [a.arg for a in [*args.posonlyargs, *args.args]]
    if args.vararg:
        names.append("*" + args.vararg.arg)
    names.extend(a.arg for a in args.kwonlyargs)
    if args.kwarg:
        names.append("**" + args.kwarg.arg)
    return names


def _module_names(path: str) -> list[str]:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return [".".join(parts[i:]) for i in range(len(parts))]


class CallGraph:
    """Functions, call sites and value references across a set of modules."""

    def __init__(self, modules: dict[str, ModuleInfo]) -> None:
        self.modules = modules
        self.functions: dict[str, FunctionInfo] = {}
        self.nodes: dict[str, FunctionNode] = {}
        self.call_sites: list[CallSite] = []
        # Function ids referenced as values (callbacks, decorators) rather than called.
        self.value_refs: dict[str, list[str]] = defaultdict(list)
        self._module_index: dict[str, set[str]] = defaultdict(set)
        self._methods_by_name: dict[str, list[str]] = defaultdict(list)
        self._build()

    # -- construction -------------------------------------------------

    def _build(self) -> None:
        for path, module in self.modules.items():
            if not module.ok:
                continue
            for name in _module_names(path):
                self._module_index[name].add(path)
            self._index_functions(module)
        for module in self.modules.values():
            if module.ok:
                self._index_calls(module)

    def _index_functions(self, module: ModuleInfo) -> None:
        assert module.tree is not None
        uses_framework = any(
            q.split(".")[0] in ASYNC_FRAMEWORK_ROOTS for q in module.imports.values()
        )
        in_test = is_test_path(module.path)
        for node in ast.walk(module.tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            qualname = module.qualname(node)
            cls = module.enclosing_class(node)
            decorators = []
            for dec in node.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                decorators.append(module.resolve(target) or module.dotted(target) or "<expr>")
            info = FunctionInfo(
                id=f"{module.path}::{qualname}",
                file=module.path,
                qualname=qualname,
                name=node.name,
                class_name=cls.name if cls else None,
                is_async=isinstance(node, ast.AsyncFunctionDef),
                line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                params=function_params(node),
                decorators=decorators,
                is_generator=_is_generator(node),
                nested=module.enclosing_function(node) is not None,
                in_test=in_test,
                class_has_bases=bool(cls and cls.bases),
                module_uses_async_framework=uses_framework,
            )
            self.functions[info.id] = info
            self.nodes[info.id] = node
            if cls is not None:
                self._methods_by_name[node.name].append(info.id)

    def _owner(self, module: ModuleInfo, node: ast.AST) -> tuple[FunctionNode | None, bool, bool]:
        """Innermost enclosing def, plus whether a lambda or generator expression intervenes."""
        in_lambda = in_genexp = False
        current = module.parents.get(node)
        while current is not None:
            if isinstance(current, ast.Lambda):
                in_lambda = True
            elif isinstance(current, ast.GeneratorExp):
                in_genexp = True
            elif isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return current, in_lambda, in_genexp
            current = module.parents.get(current)
        return None, in_lambda, in_genexp

    def _index_calls(self, module: ModuleInfo) -> None:
        assert module.tree is not None
        in_test = is_test_path(module.path)
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Call):
                owner, in_lambda, in_genexp = self._owner(module, node)
                owner_id = f"{module.path}::{module.qualname(owner)}" if owner else None
                qualified = module.resolve(node.func)
                if qualified and owner_id and not in_lambda:
                    kind = SUPPORTED_BLOCKING.get(qualified) or (
                        "unsupported" if qualified in UNSUPPORTED_BLOCKING else None
                    )
                    if kind:
                        self.functions[owner_id].blocking.append(
                            BlockingCall(
                                qualified,
                                kind,
                                node.lineno,
                                module.char_col(node.func.lineno, node.func.col_offset),
                            )
                        )
                callee, heuristic = self.resolve_callee(module, node.func, owner)
                if callee is None:
                    continue
                parent = module.parents.get(node)
                wrapped = (
                    isinstance(parent, ast.Call)
                    and node in parent.args
                    and module.resolve(parent.func) in AWAITABLE_WRAPPERS
                )
                self.call_sites.append(
                    CallSite(
                        caller=owner_id,
                        callee=callee,
                        file=module.path,
                        line=node.func.lineno,
                        column=module.char_col(node.func.lineno, node.func.col_offset),
                        awaited=isinstance(parent, ast.Await),
                        wrapped=wrapped,
                        in_lambda=in_lambda,
                        in_genexp=in_genexp,
                        heuristic=heuristic,
                        in_test=in_test,
                    )
                )
            elif isinstance(node, (ast.Name, ast.Attribute)) and isinstance(
                getattr(node, "ctx", None), ast.Load
            ):
                parent = module.parents.get(node)
                if isinstance(parent, ast.Call) and parent.func is node:
                    continue
                if isinstance(parent, ast.Attribute) and parent.value is node:
                    continue
                if isinstance(parent, ast.Call) and (module.resolve(parent.func) or "").endswith(
                    "fastapi.Depends"
                ):
                    continue
                owner, _, _ = self._owner(module, node)
                target, heuristic = self.resolve_callee(module, node, owner)
                if target is not None and not heuristic:
                    self.value_refs[target].append(f"{module.path}:{node.lineno}")

    # -- resolution ---------------------------------------------------

    def _lookup_dotted(self, dotted: str) -> str | None:
        parts = dotted.split(".")
        for split in range(len(parts) - 1, 0, -1):
            paths = self._module_index.get(".".join(parts[:split]))
            if paths and len(paths) == 1:
                candidate = f"{next(iter(paths))}::{'.'.join(parts[split:])}"
                if candidate in self.functions:
                    return candidate
        return None

    def resolve_callee(
        self, module: ModuleInfo, func: ast.AST, owner: FunctionNode | None
    ) -> tuple[str | None, bool]:
        """Map a call target to a repository function id. Second value marks a name-only guess."""
        if isinstance(func, ast.Name):
            if func.id in module.imports and func.id not in module.rebinds:
                return self._lookup_dotted(module.imports[func.id]), False
            local = f"{module.path}::{func.id}"
            return (local if local in self.functions else None), False
        if not isinstance(func, ast.Attribute):
            return None, False
        if isinstance(func.value, ast.Name) and func.value.id in ("self", "cls") and owner:
            cls = module.enclosing_class(owner)
            if cls is not None:
                candidate = f"{module.path}::{module.qualname(cls)}.{func.attr}"
                if candidate in self.functions:
                    return candidate, False
        qualified = module.resolve(func)
        if qualified:
            return self._lookup_dotted(qualified), False
        dotted = module.dotted(func)
        if dotted:
            local = f"{module.path}::{dotted}"
            if local in self.functions:
                return local, False
        candidates = self._methods_by_name.get(func.attr, [])
        if len(candidates) == 1 and func.attr not in COMMON_METHOD_NAMES:
            if not (isinstance(func.value, ast.Name) and func.value.id in module.imports):
                return candidates[0], True
        return None, False

    # -- queries ------------------------------------------------------

    def callers_of(self, function_id: str) -> list[CallSite]:
        return [site for site in self.call_sites if site.callee == function_id]

    def ambiguous_method_name(self, info: FunctionInfo) -> bool:
        return info.class_name is not None and len(self._methods_by_name.get(info.name, [])) > 1


def _is_generator(node: FunctionNode) -> bool:
    stack: list[ast.AST] = list(node.body)
    while stack:
        current = stack.pop()
        if isinstance(current, (ast.Yield, ast.YieldFrom)):
            return True
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(current))
    return False
