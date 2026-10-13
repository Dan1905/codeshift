"""Read-only repository discovery."""

from __future__ import annotations

import configparser
import os
import re
import time
import tomllib
from pathlib import Path

import pathspec

from codeshift.config import AnalysisConfig
from codeshift.discovery.manifest import FileEntry, FileStatus, Manifest, sha256_bytes
from codeshift.exceptions import UnsafePathError, ValidationError

DEFAULT_EXCLUDED_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", ".venv", "venv", "env", ".env", "__pycache__", ".mypy_cache",
        ".pytest_cache", ".ruff_cache", ".tox", ".nox", "node_modules", "build", "dist",
        "site-packages", ".eggs", ".idea", ".vscode", ".codeshift",
    }
)  # fmt: skip
CONFIG_FILES = (
    "pyproject.toml", "setup.cfg", "setup.py", "tox.ini", "pytest.ini", "mypy.ini",
    "ruff.toml", ".ruff.toml", "codeshift.toml", ".python-version",
)  # fmt: skip
DEPENDENCY_MANIFESTS = (
    "pyproject.toml", "requirements.txt", "requirements-dev.txt", "setup.py", "setup.cfg",
    "Pipfile", "poetry.lock", "uv.lock",
)  # fmt: skip
TEST_DIR_NAMES = frozenset({"tests", "test"})


def resolve_inside(root: Path, relative: str) -> Path:
    """Resolve `relative` under `root`, refusing anything that escapes the repository."""
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise UnsafePathError(f"path escapes the repository: {relative}")
    return candidate


def is_test_path(relative: str) -> bool:
    parts = Path(relative).parts
    name = parts[-1] if parts else ""
    return (
        any(part in TEST_DIR_NAMES for part in parts[:-1])
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
    )


def _spec(patterns: list[str]) -> pathspec.PathSpec:
    """Build a gitignore-style matcher across pathspec versions."""
    try:
        return pathspec.PathSpec.from_lines("gitignore", patterns)
    except (KeyError, LookupError):
        return pathspec.PathSpec.from_lines("gitwildmatch", patterns)


def _load_gitignore(root: Path) -> pathspec.PathSpec | None:
    path = root / ".gitignore"
    if not path.is_file():
        return None
    try:
        return _spec(path.read_text("utf-8").splitlines())
    except (OSError, UnicodeDecodeError):
        return None


def _detect_python_requires(root: Path) -> str | None:
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text("utf-8"))
        except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
            data = {}
        requires = data.get("project", {}).get("requires-python")
        if isinstance(requires, str):
            return requires
        poetry = data.get("tool", {}).get("poetry", {}).get("dependencies", {}).get("python")
        if isinstance(poetry, str):
            return poetry
    setup_cfg = root / "setup.cfg"
    if setup_cfg.is_file():
        parser = configparser.ConfigParser()
        try:
            parser.read(setup_cfg, encoding="utf-8")
            if parser.has_option("options", "python_requires"):
                return parser.get("options", "python_requires")
        except (configparser.Error, OSError):
            pass
    version_file = root / ".python-version"
    if version_file.is_file():
        try:
            return "==" + version_file.read_text("utf-8").strip()
        except OSError:
            pass
    return None


def minimum_python(requires: str | None) -> str | None:
    """Best-effort lower bound ('3.10') from a specifier such as '>=3.10,<4'."""
    if not requires:
        return None
    lower = re.search(r"(?:>=|==|~=|\^)\s*(3)\.(\d{1,2})", requires)
    return f"{lower.group(1)}.{lower.group(2)}" if lower else None


def discover(
    root: Path,
    analysis: AnalysisConfig | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    target_python: str | None = None,
) -> Manifest:
    """Walk `root` and describe it. Reads files, never writes."""
    started = time.perf_counter()
    analysis = analysis or AnalysisConfig()
    root = root.resolve()
    if not root.is_dir():
        raise ValidationError(f"repository path is not a directory: {root}")

    gitignore = _load_gitignore(root) if analysis.respect_gitignore else None
    include_patterns = [*analysis.include, *(include or [])]
    include_spec = _spec(include_patterns) if include_patterns else None
    exclude_spec = _spec([*analysis.exclude, *(exclude or [])])

    manifest = Manifest(root=str(root), is_git_repo=(root / ".git").exists())
    test_dirs: set[str] = set()

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        rel_dir = current.relative_to(root).as_posix()
        kept = []
        for name in sorted(dirnames):
            rel = f"{rel_dir}/{name}" if rel_dir != "." else name
            if name in DEFAULT_EXCLUDED_DIRS or name.endswith(".egg-info"):
                continue
            if (current / name / "pyvenv.cfg").exists():
                continue
            if gitignore and gitignore.match_file(rel + "/"):
                continue
            if exclude_spec.match_file(rel + "/"):
                continue
            if name in TEST_DIR_NAMES:
                test_dirs.add(rel)
            kept.append(name)
        dirnames[:] = kept

        for name in sorted(filenames):
            rel = f"{rel_dir}/{name}" if rel_dir != "." else name
            if rel_dir == ".":
                if name in CONFIG_FILES:
                    manifest.config_files.append(name)
                if name in DEPENDENCY_MANIFESTS:
                    manifest.dependency_manifests.append(name)
            if not name.endswith(".py"):
                continue
            path = current / name
            if path.is_symlink():
                manifest.skipped.append(_skip(rel, "symbolic link"))
                continue
            if gitignore and gitignore.match_file(rel):
                manifest.skipped.append(_skip(rel, "matched .gitignore"))
                continue
            if exclude_spec.match_file(rel):
                manifest.skipped.append(_skip(rel, "matched exclude pattern"))
                continue
            if include_spec and not include_spec.match_file(rel):
                manifest.skipped.append(_skip(rel, "not matched by include patterns"))
                continue
            try:
                size = path.stat().st_size
                if size > analysis.max_file_bytes:
                    manifest.skipped.append(
                        _skip(rel, f"larger than {analysis.max_file_bytes} bytes")
                    )
                    continue
                digest = sha256_bytes(path.read_bytes())
            except OSError as exc:
                manifest.skipped.append(_skip(rel, f"unreadable: {exc.strerror or exc}"))
                continue
            manifest.files.append(
                FileEntry(path=rel, sha256=digest, size=size, is_test=is_test_path(rel))
            )

    manifest.test_dirs = sorted(test_dirs)
    manifest.python_requires = _detect_python_requires(root)
    manifest.target_python = target_python or minimum_python(manifest.python_requires)
    manifest.scan_seconds = round(time.perf_counter() - started, 4)
    return manifest


def _skip(rel: str, reason: str) -> FileEntry:
    return FileEntry(path=rel, status=FileStatus.SKIPPED, reason=reason)
