from __future__ import annotations

from pathlib import Path

import pytest

from codeshift.config import Config, load_config
from codeshift.discovery.repository import discover, minimum_python, resolve_inside
from codeshift.exceptions import ConfigError, UnsafePathError


def test_discovery_respects_default_exclusions_gitignore_and_patterns(make_repo):
    repo = make_repo(
        {
            "pyproject.toml": '[project]\nname = "x"\nrequires-python = ">=3.11"\n',
            ".gitignore": "generated/\nignored.py\n",
            "src/app.py": "x = 1\n",
            "src/ignored.py": "x = 1\n",
            "generated/out.py": "x = 1\n",
            ".venv/lib/site.py": "x = 1\n",
            "node_modules/pkg/tool.py": "x = 1\n",
            "__pycache__/app.cpython-312.py": "x = 1\n",
            "tests/test_app.py": "def test_x(): pass\n",
            "vendor/lib.py": "x = 1\n",
            "notes.txt": "hello\n",
        }
    )
    manifest = discover(repo, exclude=["vendor/**"])

    assert [f.path for f in manifest.files] == ["src/app.py", "tests/test_app.py"]
    skipped = {f.path: f.reason for f in manifest.skipped}
    assert skipped["src/ignored.py"] == "matched .gitignore"
    assert manifest.python_requires == ">=3.11"
    assert manifest.target_python == "3.11"
    assert manifest.test_dirs == ["tests"]
    assert "pyproject.toml" in manifest.config_files
    assert manifest.files[1].is_test is True


def test_include_patterns_limit_the_scan(make_repo):
    repo = make_repo({"src/a.py": "a = 1\n", "scripts/b.py": "b = 1\n"})
    manifest = discover(repo, include=["src/**"])
    assert [f.path for f in manifest.files] == ["src/a.py"]
    assert manifest.skipped[0].reason == "not matched by include patterns"


def test_discovery_does_not_modify_the_repository(make_repo):
    repo = make_repo({"a.py": "import os\n", "sub/b.py": "x = 1\n"})
    before = {p: p.stat().st_mtime_ns for p in repo.rglob("*")}
    discover(repo)
    assert {p: p.stat().st_mtime_ns for p in repo.rglob("*")} == before


def test_resolve_inside_rejects_escaping_paths(tmp_path: Path):
    with pytest.raises(UnsafePathError):
        resolve_inside(tmp_path, "../outside.py")
    assert resolve_inside(tmp_path, "a/b.py") == (tmp_path / "a/b.py").resolve()


@pytest.mark.parametrize(
    ("spec", "expected"),
    [(">=3.10,<4", "3.10"), ("^3.9", "3.9"), ("==3.12.1", "3.12"), ("<4", None), (None, None)],
)
def test_minimum_python(spec, expected):
    assert minimum_python(spec) == expected


def test_config_defaults_work_without_any_file(tmp_path: Path):
    config = load_config(tmp_path)
    assert config == Config()
    assert config.llm.enabled is False
    assert config.verification.run_tests is False
    assert config.migrations.auto_apply_low_risk is False


def test_config_is_read_from_codeshift_toml_and_pyproject(make_repo):
    repo = make_repo(
        {"codeshift.toml": '[project]\ntarget_python = "3.11"\n[llm]\nprovider = "sample"\n'}
    )
    assert load_config(repo).project.target_python == "3.11"
    other = make_repo(
        {"pyproject.toml": "[tool.codeshift.verification]\nrun_mypy = true\n"}, name="other"
    )
    assert load_config(other).verification.run_mypy is True


@pytest.mark.parametrize(
    "content",
    [
        "[llm]\ntimeout_seconds = -1\n",
        "[project]\ntarget_python = 'three'\n",
        "[unknown_section]\nx = 1\n",
        "[llm]\nprovider = 'made-up'\n",
        "[llm]\nredact_patterns = ['(']\n",
        "not toml at all [[[",
    ],
)
def test_invalid_configuration_is_rejected_with_a_config_error(make_repo, content):
    repo = make_repo({"codeshift.toml": content})
    with pytest.raises(ConfigError):
        load_config(repo)


def test_api_key_comes_only_from_the_environment(monkeypatch):
    config = Config.model_validate({"llm": {"enabled": True, "provider": "groq"}})
    assert config.llm.resolved_api_key() is None
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_value")
    assert config.llm.resolved_api_key() == "gsk_test_value"
    assert config.llm.resolved_base_url() == "https://api.groq.com/openai/v1"
    assert "gsk_test_value" not in config.model_dump_json()
