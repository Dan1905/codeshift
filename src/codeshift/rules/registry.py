"""Rule interface and registry.

Rule metadata (`RuleSpec`) selects a dedicated implementation class; structural
rewrites are never driven by string replacement.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from codeshift.analysis.ast_analyzer import ModuleInfo
from codeshift.analysis.callgraph import CallGraph
from codeshift.config import CustomRuleConfig, RulesConfig
from codeshift.exceptions import ConfigError
from codeshift.models.findings import Finding, TransformKind
from codeshift.models.migrations import RuleSpec


@dataclass
class AnalysisContext:
    """Repository-level facts available to every detector."""

    target_python: str | None = None
    graph: CallGraph | None = None


@dataclass
class RuleTransform:
    """Result of one rule rewriting one module in memory."""

    source: str
    count: int = 0
    warnings: list[str] = field(default_factory=list)


class Rule(ABC):
    """A migration rule: metadata, a detector and optionally a deterministic rewrite."""

    spec: RuleSpec
    # Repo-wide rules are transformed by a dedicated engine, not file by file.
    repo_wide: bool = False

    @abstractmethod
    def detect(self, module: ModuleInfo, ctx: AnalysisContext) -> list[Finding]:
        """Return findings for one parsed module."""

    def transform(self, module: ModuleInfo) -> RuleTransform | None:
        """Rewrite the module, or return None when the rule is detect-only."""
        return None

    def finding(
        self,
        module: ModuleInfo,
        node: object,
        description: str,
        suggestion: str,
        *,
        confidence: float = 0.95,
        kind: TransformKind | None = None,
        symbol: str | None = None,
        needs_dependency_changes: bool = False,
    ) -> Finding:
        line = getattr(node, "lineno", 1)
        column = getattr(node, "col_offset", 0)
        return Finding(
            id=Finding.make_id(self.spec.id, module.path, line, column),
            file=module.path,
            start_line=line,
            end_line=getattr(node, "end_lineno", line) or line,
            column=column,
            rule_id=self.spec.id,
            severity=self.spec.severity,
            confidence=confidence,
            excerpt=module.excerpt(node),  # type: ignore[arg-type]
            description=description,
            suggestion=suggestion,
            transform_kind=kind or self.spec.transform_kind,
            needs_dependency_changes=needs_dependency_changes,
            symbol=symbol,
        )

    def applies_to_target(self, target_python: str | None) -> bool:
        """Respect `min_target_python`. Unknown targets do not disable a rule."""
        minimum = self.spec.min_target_python
        if minimum is None or target_python is None:
            return True
        return _version(target_python) >= _version(minimum)


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


class RuleRegistry:
    """Holds built-in and project rules and resolves user selections."""

    def __init__(self) -> None:
        self._rules: dict[str, Rule] = {}

    def register(self, rule: Rule) -> None:
        if rule.spec.id in self._rules:
            raise ConfigError(f"duplicate rule id: {rule.spec.id}")
        self._rules[rule.spec.id] = rule

    def all(self) -> list[Rule]:
        return list(self._rules.values())

    def get(self, rule_id: str) -> Rule:
        try:
            return self._rules[rule_id]
        except KeyError:
            raise ConfigError(f"unknown rule: {rule_id}") from None

    def packs(self) -> dict[str, list[Rule]]:
        packs: dict[str, list[Rule]] = {}
        for rule in self._rules.values():
            packs.setdefault(rule.spec.pack, []).append(rule)
        return packs

    def select(
        self,
        selection: list[str] | None = None,
        config: RulesConfig | None = None,
        target_python: str | None = None,
    ) -> list[Rule]:
        """Resolve pack names / rule ids, then apply config enablement and version constraints."""
        config = config or RulesConfig()
        packs = self.packs()
        wanted = selection or config.enable
        chosen: dict[str, Rule] = {}
        if not wanted:
            chosen = dict(self._rules)
        for token in wanted:
            if token in packs:
                chosen.update({r.spec.id: r for r in packs[token]})
            elif token in self._rules:
                chosen[token] = self._rules[token]
            else:
                known = ", ".join(sorted([*packs, *self._rules]))
                raise ConfigError(f"unknown rule or pack '{token}'. Available: {known}")
        for token in config.disable:
            if token in packs:
                for rule in packs[token]:
                    chosen.pop(rule.spec.id, None)
            elif token in self._rules:
                chosen.pop(token, None)
            else:
                raise ConfigError(f"rules.disable names an unknown rule or pack: {token}")
        return [r for r in chosen.values() if r.applies_to_target(target_python)]


def build_registry(custom: list[CustomRuleConfig] | None = None) -> RuleRegistry:
    """Create a registry with the built-in packs and any project-specific rules."""
    from codeshift.rules.builtins import builtin_rules
    from codeshift.rules.builtins.rename import custom_rule

    registry = RuleRegistry()
    for rule in builtin_rules():
        registry.register(rule)
    for definition in custom or []:
        registry.register(custom_rule(definition))
    return registry
