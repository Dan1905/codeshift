"""Project configuration: `codeshift.toml`, `[tool.codeshift]` and environment variables."""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic import ValidationError as PydanticValidationError

from codeshift.exceptions import ConfigError

CONFIG_FILENAME = "codeshift.toml"

PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "openai/gpt-oss-120b",
        "key_env": "GROQ_API_KEY",
    },
    "openai-compatible": {"base_url": "", "model": "", "key_env": "CODESHIFT_LLM_API_KEY"},
    "sample": {"base_url": "", "model": "codeshift-sample-heuristic", "key_env": ""},
}


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectConfig(_Section):
    name: str = ""
    target_python: str | None = None

    @field_validator("target_python")
    @classmethod
    def _check_version(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"3\.\d{1,2}", value):
            raise ValueError("target_python must look like '3.12'")
        return value


class AnalysisConfig(_Section):
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    respect_gitignore: bool = True
    max_file_bytes: int = Field(default=2_000_000, gt=0)


class CustomRuleConfig(_Section):
    """A project-specific rule. `rename-call` renames a fully qualified callable."""

    id: str
    kind: Literal["rename-call"] = "rename-call"
    source: str
    target: str
    description: str = ""
    risk: Literal["low", "medium", "high"] = "medium"

    @field_validator("source", "target")
    @classmethod
    def _dotted(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)+", value):
            raise ValueError("must be a dotted name such as 'package.function'")
        return value


class RulesConfig(_Section):
    enable: list[str] = Field(default_factory=list)
    disable: list[str] = Field(default_factory=list)
    custom: list[CustomRuleConfig] = Field(default_factory=list)


class MigrationsConfig(_Section):
    auto_apply_low_risk: bool = False
    require_review_for_llm_changes: bool = True
    max_files_per_run: int = Field(default=200, gt=0)


class VerificationConfig(_Section):
    run_tests: bool = False
    # When tests are required but were not executed the run is reported as incomplete.
    require_tests: bool = True
    run_ruff: bool = True
    run_mypy: bool = False
    fail_on_test_failure: bool = True
    baseline: bool = True
    test_command: list[str] = Field(default_factory=list)
    test_timeout_seconds: int = Field(default=600, gt=0)
    isolation: Literal["none", "docker"] = "none"
    docker_image: str = "codeshift-ai"


class LLMConfig(_Section):
    enabled: bool = False
    provider: Literal["groq", "openai-compatible", "sample"] = "groq"
    model: str = ""
    base_url: str = ""
    api_key_env: str = ""
    timeout_seconds: float = Field(default=60, gt=0)
    max_retries: int = Field(default=2, ge=0, le=10)
    max_context_chars: int = Field(default=24_000, gt=0)
    max_output_tokens: int = Field(default=4_000, gt=0)
    max_requests_per_run: int = Field(default=25, gt=0)
    max_total_tokens: int = Field(default=200_000, gt=0)
    fallback: Literal["none", "sample"] = "none"
    redact_patterns: list[str] = Field(default_factory=list)

    @field_validator("redact_patterns")
    @classmethod
    def _compile(cls, value: list[str]) -> list[str]:
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid redaction regex {pattern!r}: {exc}") from exc
        return value

    def resolved_model(self) -> str:
        return (
            os.environ.get("CODESHIFT_LLM_MODEL")
            or self.model
            or PROVIDER_DEFAULTS[self.provider]["model"]
        )

    def resolved_base_url(self) -> str:
        return (
            os.environ.get("CODESHIFT_LLM_BASE_URL")
            or self.base_url
            or PROVIDER_DEFAULTS[self.provider]["base_url"]
        )

    def resolved_api_key(self) -> str | None:
        """Read the credential from the environment. It is never stored in config."""
        names = [
            self.api_key_env,
            PROVIDER_DEFAULTS[self.provider]["key_env"],
            "CODESHIFT_LLM_API_KEY",
        ]
        for name in names:
            if name and os.environ.get(name):
                return os.environ[name]
        return None


class Config(_Section):
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    rules: RulesConfig = Field(default_factory=RulesConfig)
    migrations: MigrationsConfig = Field(default_factory=MigrationsConfig)
    verification: VerificationConfig = Field(default_factory=VerificationConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read configuration: {exc}") from exc


def load_config(repo: Path, explicit: Path | None = None) -> Config:
    """Load configuration for `repo`.

    Order: an explicit `--config` file, then `codeshift.toml` in the repository,
    then `[tool.codeshift]` in its `pyproject.toml`, then defaults.
    """
    data: dict[str, Any] = {}
    source = "defaults"
    if explicit is not None:
        if not explicit.is_file():
            raise ConfigError(f"configuration file not found: {explicit}")
        data, source = _read_toml(explicit), str(explicit)
    elif (repo / CONFIG_FILENAME).is_file():
        data, source = _read_toml(repo / CONFIG_FILENAME), str(repo / CONFIG_FILENAME)
    elif (repo / "pyproject.toml").is_file():
        tool = _read_toml(repo / "pyproject.toml").get("tool", {})
        if isinstance(tool, dict) and isinstance(tool.get("codeshift"), dict):
            data, source = tool["codeshift"], f"{repo / 'pyproject.toml'} [tool.codeshift]"
    try:
        return Config.model_validate(data)
    except PydanticValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigError(f"invalid configuration in {source}: {problems}") from exc


def state_dir() -> Path:
    """Directory for the history database and run artifacts, outside target repositories."""
    return Path(os.environ.get("CODESHIFT_HOME") or Path.home() / ".codeshift").expanduser()
