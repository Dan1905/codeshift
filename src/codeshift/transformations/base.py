"""Shared transformation helpers."""

from __future__ import annotations

import ast
import difflib

from codeshift.models.migrations import ChangeLocation


def changed_ranges(before: str, after: str) -> list[tuple[int, int]]:
    """Line ranges of `before` (1-based, inclusive) touched when turning it into `after`."""
    matcher = difflib.SequenceMatcher(a=before.splitlines(), b=after.splitlines(), autojunk=False)
    ranges = []
    for tag, i1, i2, _j1, _j2 in matcher.get_opcodes():
        if tag != "equal":
            ranges.append((i1 + 1, max(i1 + 1, i2)))
    return ranges


def locations_for(rule_id: str, before: str, after: str) -> list[ChangeLocation]:
    return [
        ChangeLocation(rule_id=rule_id, start_line=start, end_line=end)
        for start, end in changed_ranges(before, after)
    ]


def syntax_error(source: str, filename: str = "<proposed>") -> str | None:
    """Return a description of the syntax error in `source`, or None when it parses."""
    try:
        ast.parse(source, filename=filename)
    except SyntaxError as exc:
        return f"{exc.msg} (line {exc.lineno})"
    except (ValueError, RecursionError) as exc:
        return str(exc)
    return None
