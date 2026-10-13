from __future__ import annotations

import pytest

from codeshift.analysis.ast_analyzer import parse_module
from codeshift.config import CustomRuleConfig, RulesConfig
from codeshift.exceptions import ConfigError
from codeshift.models.findings import TransformKind
from codeshift.rules.registry import AnalysisContext, build_registry
from codeshift.transformations.deterministic import apply_rules
from tests.conftest import FIXTURES


def detect(source: str, target: str | None = None) -> list[tuple[str, int]]:
    module = parse_module("mod.py", source)
    ctx = AnalysisContext(target_python=target)
    return sorted(
        (f.rule_id, f.start_line)
        for rule in build_registry().all()
        for f in rule.detect(module, ctx)
    )


def transform(source: str) -> str:
    return apply_rules("mod.py", source, build_registry().all()).modified_source


def test_fixture_matches_expected_output_exactly():
    before = (FIXTURES / "before" / "deprecated_apis.py").read_text()
    expected = (FIXTURES / "expected" / "deprecated_apis.py").read_text()
    result = apply_rules("deprecated_apis.py", before, build_registry().all())
    assert result.modified_source == expected
    assert result.applied_rules == [
        "python.datetime.utcnow",
        "python.collections.abc",
        "python.logging.warn",
        "python.oserror-aliases",
    ]
    assert result.requires_review is True  # utcnow changes naive -> aware
    assert result.original_hash != result.proposed_hash


def test_detection_uses_imports_not_names():
    source = (
        "from datetime import datetime\n"
        "import logging\n"
        "class Clock:\n"
        "    def utcnow(self): return 0\n"
        "def f(clock, logger):\n"
        "    clock.utcnow()\n"  # unrelated object
        "    logger.warn('x')\n"  # parameter, not a known logging.Logger
        "    return datetime.utcnow()\n"
    )
    assert detect(source) == [("python.datetime.utcnow", 8)]


def test_shadowed_module_name_is_not_reported():
    source = "import datetime\ndef f(datetime):\n    return datetime.datetime.utcnow()\n"
    assert detect(source) == []
    assert transform(source) == source


def test_unrelated_code_is_left_byte_for_byte_identical():
    source = "import os\n\n\ndef f(x):  # comment\n    return   x+1\n"
    result = apply_rules("mod.py", source, build_registry().all())
    assert result.modified_source == source
    assert result.status.value == "unchanged"


def test_utcnow_skips_when_timezone_name_is_taken():
    source = "from datetime import datetime\ntimezone = 'Europe/Paris'\nnow = datetime.utcnow()\n"
    result = apply_rules("mod.py", source, build_registry().all())
    assert result.modified_source == source
    assert any("'timezone' is already used" in w for w in result.warnings)


def test_imports_are_not_duplicated():
    source = (
        "from datetime import datetime, timezone\na = datetime.utcnow()\nb = datetime.utcnow()\n"
    )
    out = transform(source)
    assert out.count("timezone") == 3
    assert out.count("import") == 1


def test_collections_abc_split_and_whole_import():
    assert transform("from collections import Mapping, Sequence\n") == (
        "from collections.abc import Mapping, Sequence\n"
    )
    assert transform("from collections import deque, Mapping\n") == (
        "from collections import deque\nfrom collections.abc import Mapping\n"
    )
    assert transform("from collections import deque\n") == "from collections import deque\n"


def test_collections_attribute_form_is_detect_only():
    module = parse_module("mod.py", "import collections\nx = collections.Mapping\n")
    findings = build_registry().get("python.collections.abc").detect(module, AnalysisContext())
    assert [f.transform_kind for f in findings] == [TransformKind.MANUAL]


def test_logging_warn_from_import_updates_the_import():
    assert transform("from logging import warn\nwarn('x')\n") == (
        "from logging import warning\n\nwarning('x')\n"
    )


def test_oserror_alias_ignores_locally_defined_names():
    source = "class IOError(Exception):\n    pass\ntry:\n    pass\nexcept IOError:\n    pass\n"
    assert detect(source) == []
    assert transform(source) == source


def test_bare_except_is_detected_but_never_rewritten():
    source = "try:\n    pass\nexcept:\n    pass\n"
    assert detect(source) == [("python.except.bare", 3)]
    assert transform(source) == source


def test_target_version_syntax_rule():
    source = "match command:\n    case 'go':\n        pass\n"
    assert [rule for rule, _line in detect(source, target="3.9")] == [
        "python.syntax.target-version"
    ]
    assert detect(source, target="3.12") == []


def test_registry_selection_packs_ids_and_disable():
    registry = build_registry()
    assert set(registry.packs()) == {"deprecated-api", "python-modernization", "sync-to-async"}
    ids = {r.spec.id for r in registry.select(["deprecated-api"])}
    assert ids == {"python.datetime.utcnow", "python.collections.abc", "python.logging.warn"}
    disabled = registry.select(None, RulesConfig(disable=["sync-to-async", "python.except.bare"]))
    assert not any(r.repo_wide for r in disabled)
    assert "python.except.bare" not in {r.spec.id for r in disabled}
    with pytest.raises(ConfigError):
        registry.select(["no-such-pack"])


def test_custom_project_rule_renames_only_the_qualified_call():
    rule = CustomRuleConfig(id="acme.fetch", source="acme.fetch_all", target="acme.fetch_page")
    registry = build_registry([rule])
    source = "import acme\nimport other\nacme.fetch_all()\nother.fetch_all()\n"
    out = apply_rules("mod.py", source, [registry.get("acme.fetch")]).modified_source
    assert out == "import acme\nimport other\nacme.fetch_page()\nother.fetch_all()\n"
    with pytest.raises(ConfigError):
        build_registry([CustomRuleConfig(id="bad", source="a.old", target="b.new")])


def test_every_rule_documents_its_metadata():
    for rule in build_registry().all():
        spec = rule.spec
        assert spec.description and spec.references and spec.semantic_notes, spec.id
        if spec.safe_for_auto_apply:
            assert spec.risk.value == "low" and not spec.requires_review, spec.id
