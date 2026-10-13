"""Shared fixtures. No test talks to an external LLM API."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from codeshift.llm.base import LLMProvider, LLMReply
from codeshift.llm.schemas import MigrationRequest
from codeshift.storage.database import RunStore

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
LEGACY_SERVICE = ROOT / "examples" / "legacy_service"


class ScriptedProvider(LLMProvider):
    """Mock provider that replays canned replies and records what it was sent."""

    name = "scripted"
    model = "scripted-model"
    external = True

    def __init__(self, replies: list[str | Exception]) -> None:
        self.replies = list(replies)
        self.requests: list[MigrationRequest] = []
        self.feedback: list[str | None] = []

    def generate_migration(
        self, request: MigrationRequest, feedback: str | None = None
    ) -> LLMReply:
        self.requests.append(request)
        self.feedback.append(feedback)
        reply = self.replies.pop(0) if self.replies else "{}"
        if isinstance(reply, Exception):
            raise reply
        return LLMReply(content=reply, model=self.model, prompt_tokens=10, completion_tokens=20)


@pytest.fixture(autouse=True)
def isolated_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Point CodeShift's state directory at a temp dir and clear provider credentials."""
    home = tmp_path_factory.mktemp("codeshift-home")
    monkeypatch.setenv("CODESHIFT_HOME", str(home))
    for name in (
        "GROQ_API_KEY",
        "CODESHIFT_LLM_API_KEY",
        "CODESHIFT_LLM_MODEL",
        "CODESHIFT_LLM_BASE_URL",
        "CODESHIFT_API_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    return home


@pytest.fixture()
def store(isolated_home: Path) -> RunStore:
    return RunStore(isolated_home)


@pytest.fixture()
def make_repo(tmp_path: Path):
    """Create a throwaway repository from a {relative path: content} mapping."""

    def _make(files: dict[str, str], name: str = "repo") -> Path:
        root = tmp_path / name
        for relative, content in files.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, "utf-8")
        root.mkdir(parents=True, exist_ok=True)
        return root

    return _make


@pytest.fixture()
def service_repo(tmp_path: Path) -> Path:
    """A private copy of the bundled legacy microservice."""
    destination = tmp_path / "legacy_service"
    shutil.copytree(
        LEGACY_SERVICE,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
    )
    return destination
