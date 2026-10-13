"""SQLite schema versions for the history database."""

from __future__ import annotations

import sqlite3

SCHEMA: list[str] = [
    # Version 1
    """
    CREATE TABLE runs (
        id TEXT PRIMARY KEY,
        repo TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        status TEXT NOT NULL,
        record TEXT NOT NULL
    );
    CREATE INDEX runs_created ON runs (created_at);
    CREATE TABLE changes (
        run_id TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
        file TEXT NOT NULL,
        original_hash TEXT NOT NULL,
        proposed_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        PRIMARY KEY (run_id, file)
    );
    CREATE TABLE analysis_cache (
        cache_key TEXT PRIMARY KEY,
        findings TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """,
]


def migrate(connection: sqlite3.Connection) -> None:
    """Bring the database up to the latest schema version."""
    current = connection.execute("PRAGMA user_version").fetchone()[0]
    for version, script in enumerate(SCHEMA, start=1):
        if version > current:
            connection.executescript(script)
            connection.execute(f"PRAGMA user_version = {version}")
    connection.commit()
