"""Pack `deprecated-api`: standard-library APIs with documented deprecations."""

from __future__ import annotations

import ast

import libcst as cst
from libcst.metadata import QualifiedNameProvider

from codeshift.analysis.ast_analyzer import ModuleInfo
from codeshift.analysis.cst_analyzer import CountingTransformer, rewrite
from codeshift.models.findings import Finding, Severity, TransformKind
from codeshift.models.migrations import Risk, RuleSpec
from codeshift.rules.builtins.rename import RenameCallRule
from codeshift.rules.registry import AnalysisContext, Rule, RuleTransform

PACK = "deprecated-api"

UTC_CALLS = {
    "datetime.datetime.utcnow": "now",
    "datetime.datetime.utcfromtimestamp": "fromtimestamp",
}

COLLECTIONS_ABCS = frozenset(
    {
        "Awaitable", "Coroutine", "AsyncIterable", "AsyncIterator", "AsyncGenerator", "Hashable",
        "Iterable", "Iterator", "Generator", "Reversible", "Sized", "Container", "Callable",
        "Collection", "Set", "MutableSet", "Mapping", "MutableMapping", "MappingView",
        "KeysView", "ItemsView", "ValuesView", "Sequence", "MutableSequence", "ByteString",
    }
)  # fmt: skip


# -- datetime.utcnow / utcfromtimestamp --------------------------------------


class _UtcTransformer(CountingTransformer):
    METADATA_DEPENDENCIES = (QualifiedNameProvider,)

    def __init__(self, timezone_name_free: bool) -> None:
        super().__init__()
        self.timezone_name_free = timezone_name_free

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.BaseExpression:
        func = original_node.func
        if not isinstance(func, cst.Attribute) or not isinstance(updated_node.func, cst.Attribute):
            return updated_node
        matched = next((q for q in self.qualified_names(func) if q in UTC_CALLS), None)
        if matched is None:
            return updated_node
        if any(arg.star for arg in original_node.args):
            self.warnings.append(f"skipped {matched}() call that uses argument unpacking")
            return updated_node
        expected_args = 0 if matched.endswith("utcnow") else 1
        if len(original_node.args) != expected_args or any(a.keyword for a in original_node.args):
            self.warnings.append(f"skipped {matched}() call with an unexpected signature")
            return updated_node

        base = updated_node.func.value
        utc: cst.BaseExpression
        if isinstance(base, cst.Attribute):
            # `import datetime` style: reuse the module expression, no new import needed.
            utc = cst.Attribute(
                value=cst.Attribute(value=base.value, attr=cst.Name("timezone")),
                attr=cst.Name("utc"),
            )
        elif isinstance(base, cst.Name):
            if not self.timezone_name_free:
                self.warnings.append(
                    f"skipped {matched}(): the name 'timezone' is already used for something else"
                )
                return updated_node
            utc = cst.Attribute(value=cst.Name("timezone"), attr=cst.Name("utc"))
            self.add_imports.add(("datetime", "timezone"))
        else:
            return updated_node

        self.count += 1
        new_func = updated_node.func.with_changes(attr=cst.Name(UTC_CALLS[matched]))
        if expected_args == 0:
            return updated_node.with_changes(func=new_func, args=[cst.Arg(value=utc)])
        first = updated_node.args[0].with_changes(
            comma=cst.Comma(whitespace_after=cst.SimpleWhitespace(" "))
        )
        tz = cst.Arg(
            value=utc,
            keyword=cst.Name("tz"),
            equal=cst.AssignEqual(
                whitespace_before=cst.SimpleWhitespace(""),
                whitespace_after=cst.SimpleWhitespace(""),
            ),
        )
        return updated_node.with_changes(func=new_func, args=[first, tz])


class UtcNowRule(Rule):
    spec = RuleSpec(
        id="python.datetime.utcnow",
        name="Replace naive UTC datetime constructors",
        description=(
            "datetime.utcnow() and datetime.utcfromtimestamp() are deprecated since Python 3.12 "
            "and return naive datetimes."
        ),
        pack=PACK,
        severity=Severity.MEDIUM,
        risk=Risk.MEDIUM,
        safe_for_auto_apply=False,
        requires_review=True,
        deprecated_in="3.12",
        semantic_notes=(
            "The replacement returns a timezone-aware datetime. Comparing or subtracting it with "
            "naive datetimes raises TypeError, and isoformat() gains a '+00:00' suffix. "
            "datetime.timezone.utc is used instead of datetime.UTC so the result runs on every "
            "supported Python 3 version."
        ),
        verification=["syntax", "structural", "unit_tests"],
        references=[
            "https://docs.python.org/3/library/datetime.html#datetime.datetime.utcnow",
            "https://docs.python.org/3/whatsnew/3.12.html#deprecated",
        ],
    )

    def detect(self, module: ModuleInfo, ctx: AnalysisContext) -> list[Finding]:
        assert module.tree is not None
        findings = []
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Call) and module.resolve(node.func) in UTC_CALLS:
                name = (module.resolve(node.func) or "").rsplit(".", 1)[1]
                findings.append(
                    self.finding(
                        module,
                        node,
                        f"datetime.{name}() is deprecated since Python 3.12 and returns a naive "
                        "datetime.",
                        f"Use datetime.{UTC_CALLS[module.resolve(node.func) or '']}"
                        "(... timezone.utc) to get an aware UTC datetime.",
                    )
                )
        return findings

    def transform(self, module: ModuleInfo) -> RuleTransform:
        free = module.imports.get("timezone") in (None, "datetime.timezone") and (
            "timezone" not in module.rebinds
        )
        transformer = _UtcTransformer(timezone_name_free=free)
        source, count = rewrite(module.source, transformer)
        return RuleTransform(source=source, count=count, warnings=transformer.warnings)


# -- collections ABC aliases -------------------------------------------------


class _CollectionsAbcTransformer(CountingTransformer):
    def leave_SimpleStatementLine(
        self, original_node: cst.SimpleStatementLine, updated_node: cst.SimpleStatementLine
    ) -> cst.BaseStatement | cst.FlattenSentinel[cst.BaseStatement]:
        if len(updated_node.body) != 1:
            return updated_node
        stmt = updated_node.body[0]
        if not (
            isinstance(stmt, cst.ImportFrom)
            and not stmt.relative
            and isinstance(stmt.module, cst.Name)
            and stmt.module.value == "collections"
            and not isinstance(stmt.names, cst.ImportStar)
        ):
            return updated_node
        abcs = [a for a in stmt.names if a.name.value in COLLECTIONS_ABCS]
        others = [a for a in stmt.names if a.name.value not in COLLECTIONS_ABCS]
        if not abcs:
            return updated_node
        self.count += len(abcs)
        abc_module = cst.Attribute(value=cst.Name("collections"), attr=cst.Name("abc"))
        if not others:
            return updated_node.with_changes(body=[stmt.with_changes(module=abc_module)])
        keep = stmt.with_changes(names=_strip_last_comma(others))
        moved = cst.SimpleStatementLine(
            body=[
                cst.ImportFrom(
                    module=abc_module,
                    names=_strip_last_comma(abcs),
                    lpar=stmt.lpar,
                    rpar=stmt.rpar,
                )
            ]
        )
        return cst.FlattenSentinel([updated_node.with_changes(body=[keep]), moved])


def _strip_last_comma(aliases: list[cst.ImportAlias]) -> list[cst.ImportAlias]:
    return [*aliases[:-1], aliases[-1].with_changes(comma=cst.MaybeSentinel.DEFAULT)]


class CollectionsAbcRule(Rule):
    spec = RuleSpec(
        id="python.collections.abc",
        name="Import ABCs from collections.abc",
        description=(
            "Abstract base classes such as Mapping were removed from the top-level collections "
            "module in Python 3.10."
        ),
        pack=PACK,
        severity=Severity.HIGH,
        risk=Risk.LOW,
        safe_for_auto_apply=True,
        requires_review=False,
        deprecated_in="3.3",
        removed_in="3.10",
        semantic_notes="collections.abc exposes the same classes; behaviour is unchanged.",
        references=["https://docs.python.org/3/whatsnew/3.10.html#removed"],
    )

    def detect(self, module: ModuleInfo, ctx: AnalysisContext) -> list[Finding]:
        assert module.tree is not None
        findings = []
        for node in ast.walk(module.tree):
            if isinstance(node, ast.ImportFrom) and node.module == "collections" and not node.level:
                names = [a.name for a in node.names if a.name in COLLECTIONS_ABCS]
                if names:
                    findings.append(
                        self.finding(
                            module,
                            node,
                            f"{', '.join(names)} imported from collections; removed in Python 3.10.",
                            f"Import {', '.join(names)} from collections.abc.",
                        )
                    )
            elif (
                isinstance(node, ast.Attribute)
                and node.attr in COLLECTIONS_ABCS
                and module.resolve(node) == f"collections.{node.attr}"
            ):
                findings.append(
                    self.finding(
                        module,
                        node,
                        f"collections.{node.attr} was removed in Python 3.10.",
                        f"Use collections.abc.{node.attr}.",
                        kind=TransformKind.MANUAL,
                        confidence=0.9,
                    )
                )
        return findings

    def transform(self, module: ModuleInfo) -> RuleTransform:
        transformer = _CollectionsAbcTransformer()
        source, count = rewrite(module.source, transformer)
        return RuleTransform(source=source, count=count)


# -- logging.warn ------------------------------------------------------------


def logging_warn_rule() -> Rule:
    spec = RuleSpec(
        id="python.logging.warn",
        name="Replace logging.warn with logging.warning",
        description="warn() is a deprecated alias of warning() since Python 3.3.",
        pack=PACK,
        severity=Severity.LOW,
        risk=Risk.LOW,
        safe_for_auto_apply=True,
        requires_review=False,
        deprecated_in="3.3",
        semantic_notes=(
            "warn() forwards to warning() after emitting a DeprecationWarning, so the rename only "
            "removes that warning. Loggers are recognised when assigned at module level from "
            "logging.getLogger()."
        ),
        references=["https://docs.python.org/3/library/logging.html#logging.Logger.warning"],
    )
    return RenameCallRule(spec, "logging.warn", "logging.warning", logger_receivers=True)
