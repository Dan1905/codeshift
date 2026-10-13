"""Exception hierarchy and the stable process exit codes."""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """Stable exit codes used by every CLI command."""

    OK = 0
    INTERNAL_ERROR = 1
    VALIDATION_FAILED = 2
    VERIFICATION_FAILED = 3
    CONFIG_ERROR = 4
    FINDINGS_PRESENT = 5


class CodeShiftError(Exception):
    """Base class for all expected CodeShift failures."""

    exit_code: ExitCode = ExitCode.INTERNAL_ERROR


class ConfigError(CodeShiftError):
    """Configuration file or environment is invalid."""

    exit_code = ExitCode.CONFIG_ERROR


class ValidationError(CodeShiftError):
    """A request cannot be carried out safely (stale files, missing approval...)."""

    exit_code = ExitCode.VALIDATION_FAILED


class UnsafePathError(ValidationError):
    """A path escapes the configured repository."""


class StaleSourceError(ValidationError):
    """A file changed on disk since it was analysed or migrated."""


class RunNotFoundError(ValidationError):
    """The requested migration run does not exist."""


class TransformError(CodeShiftError):
    """A transformation could not be produced."""

    exit_code = ExitCode.VALIDATION_FAILED


class LLMError(CodeShiftError):
    """Base class for LLM provider failures."""

    exit_code = ExitCode.VALIDATION_FAILED


class LLMUnavailableError(LLMError):
    """The provider is not configured or cannot be reached."""


class LLMResponseError(LLMError):
    """The provider answered, but the answer failed validation."""


class LLMBudgetError(LLMError):
    """A configured request or token limit was reached."""


class VerificationFailed(CodeShiftError):
    """Verification finished with a non-passing overall status."""

    exit_code = ExitCode.VERIFICATION_FAILED
