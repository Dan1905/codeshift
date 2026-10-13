"""Repository manifest models."""

from __future__ import annotations

import hashlib
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FileStatus(StrEnum):
    DISCOVERED = "discovered"
    PARSED = "parsed"
    PARSE_FAILED = "parse_failed"
    SKIPPED = "skipped"


class FileEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    sha256: str = ""
    size: int = 0
    status: FileStatus = FileStatus.DISCOVERED
    reason: str = ""
    is_test: bool = False


class Manifest(BaseModel):
    """What the repository analyzer found. Building it never modifies the repository."""

    model_config = ConfigDict(extra="forbid")

    root: str
    is_git_repo: bool = False
    files: list[FileEntry] = Field(default_factory=list)
    skipped: list[FileEntry] = Field(default_factory=list)
    config_files: list[str] = Field(default_factory=list)
    dependency_manifests: list[str] = Field(default_factory=list)
    test_dirs: list[str] = Field(default_factory=list)
    python_requires: str | None = None
    target_python: str | None = None
    scan_seconds: float = 0.0

    def python_files(self) -> list[FileEntry]:
        return [f for f in self.files if f.status is not FileStatus.SKIPPED]

    @property
    def parsed_count(self) -> int:
        return sum(1 for f in self.files if f.status is FileStatus.PARSED)

    @property
    def parse_failures(self) -> list[FileEntry]:
        return [f for f in self.files if f.status is FileStatus.PARSE_FAILED]
