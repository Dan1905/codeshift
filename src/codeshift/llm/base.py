"""Provider-independent LLM interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from codeshift.llm.schemas import MigrationRequest


@dataclass(frozen=True)
class LLMReply:
    """Raw provider output. `content` is untrusted until validated."""

    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LLMProvider(ABC):
    """A source of migration proposals.

    Providers only return text. They never write files or run commands; every
    reply goes through schema and AST validation before it can become a change.
    """

    name: str = "provider"
    model: str = ""
    # Whether source code leaves this machine. Controls redaction.
    external: bool = True

    @abstractmethod
    def generate_migration(
        self, request: MigrationRequest, feedback: str | None = None
    ) -> LLMReply:
        """Return a JSON document matching `MigrationResponse` for `request`.

        `feedback` carries the validation error of a previous rejected reply.
        """
