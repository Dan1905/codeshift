"""Import-aware renaming of a qualified callable (`package.old()` -> `package.new()`)."""

from __future__ import annotations

import ast

import libcst as cst
from libcst.metadata import QualifiedNameProvider

from codeshift.analysis.ast_analyzer import ModuleInfo
from codeshift.analysis.cst_analyzer import CountingTransformer, rewrite
from codeshift.config import CustomRuleConfig
from codeshift.exceptions import ConfigError
from codeshift.models.findings import Finding, Severity, TransformKind
from codeshift.models.migrations import Risk, RuleSpec
from codeshift.rules.registry import AnalysisContext, Rule, RuleTransform


class RenameCallTransformer(CountingTransformer):
    """Rename calls that resolve to `source`, leaving same-named unrelated calls alone."""

    METADATA_DEPENDENCIES = (QualifiedNameProvider,)

    def __init__(
        self,
        source: str,
        target: str,
        receiver_names: frozenset[str] = frozenset(),
        receiver_factory: str | None = None,
    ) -> None:
        super().__init__()
        self.source = source
        self.module, _, self.old = source.rpartition(".")
        self.new = target.rpartition(".")[2]
        self.receiver_names = receiver_names
        self.receiver_factory = receiver_factory

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.BaseExpression:
        func = original_node.func
        if isinstance(func, cst.Attribute) and func.attr.value == self.old:
            matches = self.source in self.qualified_names(func)
            if not matches and isinstance(func.value, cst.Name):
                matches = func.value.value in self.receiver_names
            if not matches and self.receiver_factory and isinstance(func.value, cst.Call):
                matches = self.receiver_factory in self.qualified_names(func.value.func)
            if matches and isinstance(updated_node.func, cst.Attribute):
                self.count += 1
                return updated_node.with_changes(
                    func=updated_node.func.with_changes(attr=cst.Name(self.new))
                )
        elif isinstance(func, cst.Name) and func.value == self.old:
            if self.source in self.qualified_names(func):
                self.count += 1
                self.add_imports.add((self.module, self.new))
                self.remove_imports.add((self.module, self.old))
                return updated_node.with_changes(func=cst.Name(self.new))
        return updated_node


class RenameCallRule(Rule):
    """Deterministic rule that renames one callable inside the same module/namespace."""

    def __init__(
        self,
        spec: RuleSpec,
        source: str,
        target: str,
        *,
        logger_receivers: bool = False,
    ) -> None:
        if source.rpartition(".")[0] != target.rpartition(".")[0]:
            raise ConfigError(
                f"rule {spec.id}: rename-call needs source and target in the same module "
                f"({source} -> {target})"
            )
        self.spec = spec
        self.source = source
        self.target = target
        self.logger_receivers = logger_receivers
        self._old = source.rpartition(".")[2]
        self._new = target.rpartition(".")[2]

    def _matches(self, module: ModuleInfo, call: ast.Call) -> bool:
        if module.resolve(call.func) == self.source:
            return True
        func = call.func
        if not (self.logger_receivers and isinstance(func, ast.Attribute)):
            return False
        if func.attr != self._old:
            return False
        if isinstance(func.value, ast.Name):
            return func.value.id in module.logger_names
        return (
            isinstance(func.value, ast.Call)
            and module.resolve(func.value.func) == "logging.getLogger"
        )

    def detect(self, module: ModuleInfo, ctx: AnalysisContext) -> list[Finding]:
        assert module.tree is not None
        return [
            self.finding(
                module,
                node,
                f"Call to {self.source}(). {self.spec.description}",
                f"Call {self._new}() instead of {self._old}().",
            )
            for node in ast.walk(module.tree)
            if isinstance(node, ast.Call) and self._matches(module, node)
        ]

    def transform(self, module: ModuleInfo) -> RuleTransform:
        transformer = RenameCallTransformer(
            self.source,
            self.target,
            receiver_names=frozenset(module.logger_names) if self.logger_receivers else frozenset(),
            receiver_factory="logging.getLogger" if self.logger_receivers else None,
        )
        source, count = rewrite(module.source, transformer)
        return RuleTransform(source=source, count=count, warnings=transformer.warnings)


def custom_rule(definition: CustomRuleConfig) -> Rule:
    """Build a project-specific rule from validated configuration."""
    spec = RuleSpec(
        id=definition.id,
        name=f"Rename {definition.source} to {definition.target}",
        description=definition.description
        or f"Project rule replaces it with {definition.target}().",
        pack="custom",
        severity=Severity.MEDIUM,
        risk=Risk(definition.risk),
        transform_kind=TransformKind.DETERMINISTIC,
        safe_for_auto_apply=definition.risk == "low",
        requires_review=definition.risk != "low",
    )
    return RenameCallRule(spec, definition.source, definition.target)
