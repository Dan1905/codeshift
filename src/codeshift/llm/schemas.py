"""Structured request and response contracts for LLM-assisted migrations."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SUPPORTED_OPERATIONS = ("replace_function",)


class MigrationRequest(BaseModel):
    """Everything a provider is given for one function. Deliberately not the whole repository."""

    model_config = ConfigDict(extra="forbid")

    migration_id: str
    file: str
    target_symbol: str
    objective: str
    source_code: str
    is_async: bool
    parameters: list[str]
    decorators: list[str] = Field(default_factory=list)
    # Local name -> qualified name for the imports this function relies on.
    aliases: dict[str, str] = Field(default_factory=dict)
    imports: list[str] = Field(default_factory=list)
    blocking_calls: list[str] = Field(default_factory=list)
    callers: list[str] = Field(default_factory=list)
    async_callees: list[str] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)
    source_dependencies: dict[str, str] = Field(default_factory=dict)
    target_dependencies: dict[str, str] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)
    verification_requirements: list[str] = Field(default_factory=list)


class ProposedChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str
    operation: Literal["replace_function"]
    target_symbol: str
    replacement_code: str = Field(min_length=1)
    explanation: str = ""
    assumptions: list[str] = Field(default_factory=list)
    verification_steps: list[str] = Field(default_factory=list)
    required_imports: list[str] = Field(default_factory=list)

    @field_validator("replacement_code")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("replacement_code is empty")
        return value


class MigrationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    migration_id: str
    changes: list[ProposedChange] = Field(min_length=1)
    risks: list[str] = Field(default_factory=list)
    requires_human_review: bool = True


RESPONSE_SCHEMA_TEXT = """{
  "migration_id": "<the migration_id from the request>",
  "changes": [
    {
      "file": "<the file from the request>",
      "operation": "replace_function",
      "target_symbol": "<the target_symbol from the request>",
      "replacement_code": "<complete source of the rewritten function, starting at column 0>",
      "explanation": "<one or two sentences>",
      "assumptions": ["<assumption>"],
      "verification_steps": ["<step>"],
      "required_imports": ["import httpx"]
    }
  ],
  "risks": ["<risk>"],
  "requires_human_review": true
}"""
