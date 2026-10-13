"""Reversible secret redaction applied before source leaves the machine."""

from __future__ import annotations

import re

_ASSIGNED_SECRET = re.compile(
    r"""(?ix)
    (\b\w*(?:password|passwd|secret|token|api_?key|apikey|credential|auth)\w*
     \s*(?::\s*[\w\[\], |.]+)?\s*=\s*)      # name, optional annotation, '='
    (["'])([^"'\n]{4,})\2                    # quoted value
    """
)
_KNOWN_SHAPES = [
    re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
    re.compile(r"(?<=Bearer )[A-Za-z0-9._\-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"),
]


class Redactor:
    """Replace secrets with placeholders and restore them in validated output.

    Placeholders are valid inside string literals, so redacted code still parses
    and a replacement produced from it can be mapped back to the real values
    locally. The mapping never leaves the process.
    """

    def __init__(self, extra_patterns: list[str] | None = None) -> None:
        self._patterns = [*_KNOWN_SHAPES, *(re.compile(p) for p in extra_patterns or [])]
        self._mapping: dict[str, str] = {}
        self._reverse: dict[str, str] = {}

    @property
    def count(self) -> int:
        return len(self._mapping)

    def _placeholder(self, secret: str) -> str:
        if secret in self._reverse:
            return self._reverse[secret]
        token = f"CODESHIFT_REDACTED_{len(self._mapping) + 1}"
        self._mapping[token] = secret
        self._reverse[secret] = token
        return token

    def redact(self, text: str) -> str:
        def assigned(match: re.Match[str]) -> str:
            return f"{match.group(1)}{match.group(2)}{self._placeholder(match.group(3))}{match.group(2)}"

        text = _ASSIGNED_SECRET.sub(assigned, text)
        for pattern in self._patterns:
            text = pattern.sub(lambda m: self._placeholder(m.group(0)), text)
        return text

    def restore(self, text: str) -> str:
        for token in sorted(self._mapping, key=len, reverse=True):
            text = text.replace(token, self._mapping[token])
        return text

    def scrub(self, text: str) -> str:
        """Remove known secret values from text destined for logs or reports."""
        for secret, token in self._reverse.items():
            text = text.replace(secret, token)
        return text
