"""Unified diffs and read-only Git working-tree inspection."""

from __future__ import annotations

import difflib
import subprocess
from pathlib import Path

GIT_TIMEOUT_SECONDS = 20


def unified_diff(path: str, before: str, after: str, context: int = 3) -> str:
    """Return a unified diff of one file, using Git-style a/ and b/ prefixes."""
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=context,
    )
    out = []
    for line in lines:
        out.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(out)


def _git(repo: Path, *args: str) -> str | None:
    """Run a read-only git command. Returns None when git is unavailable or fails."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout if completed.returncode == 0 else None


def is_git_repo(repo: Path) -> bool:
    return (_git(repo, "rev-parse", "--is-inside-work-tree") or "").strip() == "true"


def head_commit(repo: Path) -> str | None:
    out = _git(repo, "rev-parse", "HEAD")
    return out.strip() if out else None


def dirty_files(repo: Path) -> list[str] | None:
    """Paths with uncommitted changes, relative to `repo`. None when not a Git repository.

    Only `git status` is used. CodeShift never resets, cleans or checks out files.
    """
    if not is_git_repo(repo):
        return None
    out = _git(repo, "status", "--porcelain", "--untracked-files=no", "--", ".")
    if out is None:
        return None
    prefix = (_git(repo, "rev-parse", "--show-prefix") or "").strip()
    files = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        if prefix and path.startswith(prefix):
            path = path[len(prefix) :]
        files.append(path)
    return sorted(files)
