"""LibCST helpers shared by every source rewrite."""

from __future__ import annotations

import libcst as cst
from libcst.codemod import CodemodContext
from libcst.codemod.visitors import AddImportsVisitor, RemoveImportsVisitor
from libcst.metadata import MetadataWrapper, QualifiedNameProvider

from codeshift.exceptions import TransformError


class CountingTransformer(cst.CSTTransformer):
    """Base transformer that records how many rewrites it made and the imports they need."""

    def __init__(self) -> None:
        super().__init__()
        self.count = 0
        self.warnings: list[str] = []
        # (module, object-or-None) pairs to add, and ones to drop if they become unused.
        self.add_imports: set[tuple[str, str | None]] = set()
        self.remove_imports: set[tuple[str, str | None]] = set()

    def qualified_names(self, node: cst.CSTNode) -> set[str]:
        return {q.name for q in self.get_metadata(QualifiedNameProvider, node, set())}


def parse_cst(source: str) -> cst.Module:
    try:
        return cst.parse_module(source)
    except cst.ParserSyntaxError as exc:
        raise TransformError(f"LibCST cannot parse source: {exc.message}") from exc
    except RecursionError as exc:
        raise TransformError("source is nested too deeply for LibCST to parse") from exc


def manage_imports(
    module: cst.Module,
    add: set[tuple[str, str | None]],
    remove: set[tuple[str, str | None]],
) -> cst.Module:
    """Add imports without duplicating them and drop imports that are now unused."""
    if not add and not remove:
        return module
    context = CodemodContext()
    for mod, obj in sorted(add, key=lambda pair: (pair[0], pair[1] or "")):
        AddImportsVisitor.add_needed_import(context, mod, obj)
    for mod, obj in sorted(remove, key=lambda pair: (pair[0], pair[1] or "")):
        RemoveImportsVisitor.remove_unused_import(context, mod, obj)
    if remove:
        module = RemoveImportsVisitor(context).transform_module(module)
    if add:
        module = AddImportsVisitor(context).transform_module(module)
    return module


def rewrite(source: str, transformer: CountingTransformer) -> tuple[str, int]:
    """Run `transformer` over `source`. Returns the original text when nothing matched."""
    wrapper = MetadataWrapper(parse_cst(source))
    updated = wrapper.visit(transformer)
    if transformer.count == 0:
        return source, 0
    updated = manage_imports(updated, transformer.add_imports, transformer.remove_imports)
    return updated.code, transformer.count
