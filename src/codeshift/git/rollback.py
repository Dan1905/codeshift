"""Hash-guarded application and rollback of file changes."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from codeshift.discovery.manifest import sha256_bytes
from codeshift.discovery.repository import resolve_inside
from codeshift.exceptions import StaleSourceError, ValidationError
from codeshift.models.migrations import ChangeStatus, FileChange
from codeshift.storage.database import ArtifactStore


def file_hash(path: Path) -> str | None:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError:
        return None


def atomic_write(path: Path, data: bytes) -> None:
    """Write through a temporary file in the same directory, keeping the file mode."""
    mode = path.stat().st_mode if path.exists() else None
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".codeshift")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _check_hashes(repo: Path, expected: dict[str, str], action: str) -> None:
    stale = []
    for rel, digest in expected.items():
        current = file_hash(resolve_inside(repo, rel))
        if current is None:
            stale.append(f"{rel} (missing or unreadable)")
        elif current != digest:
            stale.append(rel)
    if stale:
        raise StaleSourceError(
            f"refusing to {action}: file(s) changed since the run recorded them: "
            + ", ".join(stale)
        )


def _check_writable(repo: Path, files: list[str]) -> None:
    blocked = [
        rel
        for rel in files
        if not os.access(resolve_inside(repo, rel), os.W_OK)
        or not os.access(resolve_inside(repo, rel).parent, os.W_OK)
    ]
    if blocked:
        raise ValidationError(
            "cannot write to the repository (read-only mount or missing permission): "
            + ", ".join(blocked)
        )


def apply_changes(
    repo: Path, changes: list[FileChange], artifacts: ArtifactStore, run_id: str
) -> None:
    """Write proposed content for every change, or none of them.

    Every target must still match the hash recorded at planning time, so edits
    made after planning are never overwritten. Originals are backed up first.
    """
    pending = [c for c in changes if c.status is ChangeStatus.PROPOSED]
    _check_hashes(repo, {c.file: c.original_hash for c in pending}, "apply")
    _check_writable(repo, [c.file for c in pending])
    payloads = {}
    for change in pending:
        proposed = artifacts.read(run_id, "proposed", change.file)
        if sha256_bytes(proposed) != change.proposed_hash:
            raise ValidationError(f"stored proposal for {change.file} is corrupt; plan again")
        payloads[change.file] = proposed
        artifacts.write(
            run_id, "backup", change.file, resolve_inside(repo, change.file).read_bytes()
        )

    written: list[FileChange] = []
    try:
        for change in pending:
            atomic_write(resolve_inside(repo, change.file), payloads[change.file])
            written.append(change)
    except OSError as exc:
        for change in written:
            atomic_write(
                resolve_inside(repo, change.file), artifacts.read(run_id, "backup", change.file)
            )
        raise ValidationError(
            f"could not write {exc.filename or 'a file'}: {exc.strerror or exc}. "
            "No changes were left applied."
        ) from exc
    for change in pending:
        change.status = ChangeStatus.APPLIED


def rollback_changes(
    repo: Path, changes: list[FileChange], artifacts: ArtifactStore, run_id: str
) -> None:
    """Restore the originals of an applied run.

    Refuses when any file no longer matches what the run wrote, so work done
    after the migration is never discarded.
    """
    applied = [c for c in changes if c.status is ChangeStatus.APPLIED]
    _check_hashes(repo, {c.file: c.proposed_hash for c in applied}, "roll back")
    _check_writable(repo, [c.file for c in applied])
    backups = {}
    for change in applied:
        backup = artifacts.read(run_id, "backup", change.file)
        if sha256_bytes(backup) != change.original_hash:
            raise ValidationError(
                f"backup for {change.file} is missing or corrupt; cannot roll back"
            )
        backups[change.file] = backup
    for change in applied:
        atomic_write(resolve_inside(repo, change.file), backups[change.file])
        change.status = ChangeStatus.ROLLED_BACK
