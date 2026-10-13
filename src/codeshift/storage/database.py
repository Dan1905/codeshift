"""Run history in SQLite, plus on-disk artifacts kept outside target repositories."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from codeshift.config import state_dir
from codeshift.exceptions import RunNotFoundError, ValidationError
from codeshift.models.findings import Finding
from codeshift.models.reports import RunRecord
from codeshift.storage.migrations import migrate


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ArtifactStore:
    """Proposed file contents and backups for each run.

    Only files a run proposes to change are stored, and `purge` removes them.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, run_id: str, kind: str, relative: str) -> Path:
        base = (self.root / run_id / kind).resolve()
        target = (base / relative).resolve()
        if not target.is_relative_to(base):
            raise ValidationError(f"artifact path escapes the run directory: {relative}")
        return target

    def write(self, run_id: str, kind: str, relative: str, data: bytes) -> None:
        target = self._path(run_id, kind, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def read(self, run_id: str, kind: str, relative: str) -> bytes:
        target = self._path(run_id, kind, relative)
        try:
            return target.read_bytes()
        except OSError as exc:
            raise ValidationError(
                f"artifact missing for run {run_id}: {kind}/{relative}. "
                "It may have been purged; plan the migration again."
            ) from exc

    def purge(self, run_id: str) -> bool:
        target = (self.root / run_id).resolve()
        if target.is_dir() and target.is_relative_to(self.root.resolve()):
            shutil.rmtree(target)
            return True
        return False


class RunStore:
    """Persistence for migration runs."""

    def __init__(self, home: Path | None = None) -> None:
        self.home = home or state_dir()
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(self.home / "codeshift.db")
        except (OSError, sqlite3.Error) as exc:
            raise ValidationError(
                f"cannot open the CodeShift state directory {self.home}: {exc}. "
                "Set CODESHIFT_HOME to a writable location."
            ) from exc
        self._connection.execute("PRAGMA foreign_keys = ON")
        migrate(self._connection)
        self.artifacts = ArtifactStore(self.home / "runs")

    def close(self) -> None:
        self._connection.close()

    def __del__(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is not None:
            connection.close()

    def save(self, record: RunRecord) -> None:
        record.updated_at = utc_now()
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO runs (id, repo, created_at, updated_at, status, record)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    status = excluded.status,
                    record = excluded.record
                """,
                (
                    record.run_id,
                    record.plan.repo,
                    record.plan.created_at,
                    record.updated_at,
                    record.status.value,
                    record.model_dump_json(),
                ),
            )
            self._connection.execute("DELETE FROM changes WHERE run_id = ?", (record.run_id,))
            self._connection.executemany(
                "INSERT INTO changes VALUES (?, ?, ?, ?, ?)",
                [
                    (record.run_id, c.file, c.original_hash, c.proposed_hash, c.status.value)
                    for c in record.plan.changes
                ],
            )

    def get(self, run_id: str) -> RunRecord:
        row = self._connection.execute("SELECT record FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFoundError(f"no migration run with id '{run_id}'")
        return RunRecord.model_validate_json(row[0])

    def list_runs(self, limit: int = 50, repo: str | None = None) -> list[RunRecord]:
        query = "SELECT record FROM runs"
        params: list[object] = []
        if repo:
            query += " WHERE repo = ?"
            params.append(repo)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = self._connection.execute(query, params).fetchall()
        return [RunRecord.model_validate_json(row[0]) for row in rows]

    def latest_id(self) -> str | None:
        row = self._connection.execute(
            "SELECT id FROM runs ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    # -- incremental analysis cache ------------------------------------

    def cached_findings(self, key: str) -> list[Finding] | None:
        row = self._connection.execute(
            "SELECT findings FROM analysis_cache WHERE cache_key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        return [Finding.model_validate(item) for item in json.loads(row[0])]

    def store_findings(self, entries: dict[str, list[Finding]]) -> None:
        if not entries:
            return
        now = utc_now()
        with self._connection:
            self._connection.executemany(
                "INSERT OR REPLACE INTO analysis_cache VALUES (?, ?, ?)",
                [
                    (key, json.dumps([f.model_dump(mode="json") for f in findings]), now)
                    for key, findings in entries.items()
                ],
            )
