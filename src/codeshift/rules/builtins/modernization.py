"""Pack `python-modernization`: small, well-defined modernisation rules."""

from __future__ import annotations

import ast

import libcst as cst
from libcst.metadata import QualifiedNameProvider

from codeshift.analysis.ast_analyzer import ModuleInfo
from codeshift.analysis.cst_analyzer import CountingTransformer, rewrite
from codeshift.models.findings import Finding, Severity, TransformKind
from codeshift.models.migrations import Risk, RuleSpec
from codeshift.rules.registry import AnalysisContext, Rule, RuleTransform

PACK = "python-modernization"
OSERROR_ALIASES = frozenset({"IOError", "EnvironmentError"})


class _OSErrorTransformer(CountingTransformer):
    METADATA_DEPENDENCIES = (QualifiedNameProvider,)

    def leave_Name(self, original_node: cst.Name, updated_node: cst.Name) -> cst.BaseExpression:
        if original_node.value in OSERROR_ALIASES:
            if f"builtins.{original_node.value}" in self.qualified_names(original_node):
                self.count += 1
                return updated_node.with_changes(value="OSError")
        return updated_node


class OSErrorAliasRule(Rule):
    spec = RuleSpec(
        id="python.oserror-aliases",
        name="Replace IOError and EnvironmentError with OSError",
        description="IOError and EnvironmentError have been plain aliases of OSError since 3.3.",
        pack=PACK,
        severity=Severity.LOW,
        risk=Risk.LOW,
        safe_for_auto_apply=True,
        requires_review=False,
        semantic_notes="The names refer to the same class object, so behaviour is identical.",
        references=["https://docs.python.org/3/library/exceptions.html#OSError"],
    )

    def detect(self, module: ModuleInfo, ctx: AnalysisContext) -> list[Finding]:
        assert module.tree is not None
        return [
            self.finding(
                module,
                node,
                f"{node.id} is a legacy alias of OSError.",
                "Use OSError.",
            )
            for node in ast.walk(module.tree)
            if isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in OSERROR_ALIASES
            and node.id not in module.rebinds
            and node.id not in module.imports
        ]

    def transform(self, module: ModuleInfo) -> RuleTransform:
        transformer = _OSErrorTransformer()
        source, count = rewrite(module.source, transformer)
        return RuleTransform(source=source, count=count)


class BareExceptRule(Rule):
    spec = RuleSpec(
        id="python.except.bare",
        name="Bare except clause",
        description="A bare `except:` also catches KeyboardInterrupt and SystemExit.",
        pack=PACK,
        severity=Severity.MEDIUM,
        risk=Risk.MEDIUM,
        transform_kind=TransformKind.MANUAL,
        semantic_notes=(
            "Detect-only. Narrowing the clause changes which exceptions propagate, so the right "
            "exception type has to be chosen by a person."
        ),
        references=["https://peps.python.org/pep-0008/#programming-recommendations"],
    )

    def detect(self, module: ModuleInfo, ctx: AnalysisContext) -> list[Finding]:
        assert module.tree is not None
        return [
            self.finding(
                module,
                node,
                "Bare `except:` catches BaseException, including KeyboardInterrupt.",
                "Catch a specific exception type, or `except Exception:` at minimum.",
                confidence=1.0,
            )
            for node in ast.walk(module.tree)
            if isinstance(node, ast.ExceptHandler) and node.type is None
        ]


class TargetVersionSyntaxRule(Rule):
    spec = RuleSpec(
        id="python.syntax.target-version",
        name="Syntax newer than the target Python version",
        description="The file uses syntax the configured target Python version cannot parse.",
        pack=PACK,
        severity=Severity.HIGH,
        risk=Risk.HIGH,
        transform_kind=TransformKind.MANUAL,
        semantic_notes=(
            "Detect-only and best effort: it relies on ast.parse(feature_version=...), which "
            "covers grammar changes but not every version-specific feature."
        ),
        references=["https://docs.python.org/3/library/ast.html#ast.parse"],
    )

    def detect(self, module: ModuleInfo, ctx: AnalysisContext) -> list[Finding]:
        if not ctx.target_python:
            return []
        minor = int(ctx.target_python.split(".")[1])
        try:
            ast.parse(module.source, feature_version=(3, minor))
        except SyntaxError as exc:
            line = exc.lineno or 1
            marker = ast.Pass(lineno=line, col_offset=0, end_lineno=line, end_col_offset=0)
            return [
                self.finding(
                    module,
                    marker,
                    f"Not valid Python {ctx.target_python} syntax: {exc.msg}.",
                    f"Rewrite the construct or raise the target version above {ctx.target_python}.",
                    confidence=0.8,
                )
            ]
        except ValueError:
            return []
        return []
