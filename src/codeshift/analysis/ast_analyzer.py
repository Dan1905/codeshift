"""Parse Python modules with `ast` and expose import-aware name resolution.

Nothing here imports or executes the code under analysis.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from codeshift.discovery.manifest import sha256_text

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass
class ModuleInfo:
    """A parsed module plus the lookup tables detectors need."""

    path: str
    source: str
    sha256: str
    tree: ast.Module | None = None
    error: str | None = None
    lines: list[str] = field(default_factory=list)
    # Local name -> fully qualified name, built from import statements.
    imports: dict[str, str] = field(default_factory=dict)
    # Names bound by something other than an import (assignment, def, parameter...).
    rebinds: set[str] = field(default_factory=set)
    # Module-level names assigned from logging.getLogger(...).
    logger_names: set[str] = field(default_factory=set)
    parents: dict[ast.AST, ast.AST] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.tree is not None

    def dotted(self, node: ast.AST) -> str | None:
        """Return 'a.b.c' for a Name/Attribute chain, else None."""
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return None
        parts.append(node.id)
        return ".".join(reversed(parts))

    def resolve(self, node: ast.AST) -> str | None:
        """Resolve a Name/Attribute chain to a qualified name through this module's imports.

        Returns None when the head is not an imported name or has been rebound,
        so a local variable that merely shares a name with a module never matches.
        """
        dotted = self.dotted(node)
        if dotted is None:
            return None
        head, _, rest = dotted.partition(".")
        if head not in self.imports or head in self.rebinds:
            return None
        return self.imports[head] + (f".{rest}" if rest else "")

    def excerpt(self, node: ast.AST, max_lines: int = 6) -> str:
        start = getattr(node, "lineno", 1)
        end = min(getattr(node, "end_lineno", start) or start, start + max_lines - 1)
        return "\n".join(self.lines[start - 1 : end])

    def char_col(self, lineno: int, byte_col: int) -> int:
        """Convert an `ast` UTF-8 byte column into a character column."""
        line = self.lines[lineno - 1] if 0 < lineno <= len(self.lines) else ""
        return len(line.encode("utf-8")[:byte_col].decode("utf-8", errors="ignore"))

    def enclosing_function(self, node: ast.AST) -> FunctionNode | None:
        current = self.parents.get(node)
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return current
            current = self.parents.get(current)
        return None

    def enclosing_class(self, node: ast.AST) -> ast.ClassDef | None:
        current = self.parents.get(node)
        while current is not None:
            if isinstance(current, ast.ClassDef):
                return current
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return None
            current = self.parents.get(current)
        return None

    def qualname(self, node: FunctionNode | ast.ClassDef) -> str:
        parts = [node.name]
        current = self.parents.get(node)
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                parts.append(current.name)
            current = self.parents.get(current)
        return ".".join(reversed(parts))


def _package_parts(path: str) -> list[str]:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    return parts[:-1]


def _collect(info: ModuleInfo, tree: ast.Module) -> None:
    package = _package_parts(info.path)
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            info.parents[child] = parent

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    info.imports[alias.asname] = alias.name
                else:
                    head = alias.name.split(".")[0]
                    info.imports[head] = head
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
                module = ".".join([*base, *(node.module.split(".") if node.module else [])])
            else:
                module = node.module or ""
            for alias in node.names:
                if alias.name != "*":
                    qualified = f"{module}.{alias.name}" if module else alias.name
                    info.imports[alias.asname or alias.name] = qualified
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            info.rebinds.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            # A method name lives in the class namespace and does not shadow module names.
            if not isinstance(node, ast.Lambda) and not isinstance(
                info.parents.get(node), ast.ClassDef
            ):
                info.rebinds.add(node.name)
            args = node.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                info.rebinds.add(arg.arg)
            for extra in (args.vararg, args.kwarg):
                if extra is not None:
                    info.rebinds.add(extra.arg)
        elif isinstance(node, ast.ClassDef):
            info.rebinds.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            info.rebinds.add(node.name)

    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
            if info.resolve(stmt.value.func) == "logging.getLogger":
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        info.logger_names.add(target.id)


def parse_module(path: str, source: str) -> ModuleInfo:
    """Parse `source`. Syntax errors are recorded on the result, not raised."""
    info = ModuleInfo(path=path, source=source, sha256=sha256_text(source))
    info.lines = source.splitlines()
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError, RecursionError) as exc:
        line = getattr(exc, "lineno", None)
        info.error = f"{type(exc).__name__}: {getattr(exc, 'msg', exc)}" + (
            f" (line {line})" if line else ""
        )
        return info
    info.tree = tree
    _collect(info, tree)
    return info


class ModuleCache:
    """Avoid re-parsing identical content within one process."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], ModuleInfo] = {}
        self.hits = 0
        self.misses = 0

    def get(self, path: str, source: str) -> ModuleInfo:
        key = (path, sha256_text(source))
        cached = self._entries.get(key)
        if cached is not None:
            self.hits += 1
            return cached
        self.misses += 1
        info = parse_module(path, source)
        self._entries[key] = info
        return info
