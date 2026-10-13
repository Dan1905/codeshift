"""Findings produced by the analysis engine."""

from __future__ import annotations

import hashlib
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TransformKind(StrEnum):
    """How a finding can be resolved."""

    DETERMINISTIC = "deterministic"
    LLM = "llm"
    MANUAL = "manual"


class Finding(BaseModel):
    """One occurrence of a rule in a source file."""

    model_config = ConfigDict(extra="forbid")

    id: str
    file: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    column: int = Field(ge=0, default=0)
    rule_id: str
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)
    excerpt: str
    description: str
    suggestion: str
    transform_kind: TransformKind
    needs_dependency_changes: bool = False
    symbol: str | None = None

    @staticmethod
    def make_id(rule_id: str, file: str, line: int, column: int) -> str:
        digest = hashlib.sha256(f"{rule_id}|{file}|{line}|{column}".encode()).hexdigest()
        return f"f_{digest[:12]}"
