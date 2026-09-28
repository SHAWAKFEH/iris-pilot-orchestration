"""Minimal forward-only SQL migrations with checksums.

Applied migrations are recorded in ops.schema_migration. Each file runs in
its own transaction. Editing an already-applied file is detected by checksum
and refused instead of being silently ignored.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import psycopg

from iris_pilot.db import MIGRATION_LOCK_KEY

BOOTSTRAP_SQL = """
CREATE SCHEMA IF NOT EXISTS ops;
CREATE TABLE IF NOT EXISTS ops.schema_migration (
    version     text        PRIMARY KEY,
    checksum    text        NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
"""


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: str
    sql: str
    checksum: str


def discover_migrations(directory: Path) -> list[Migration]:
    migrations = []
    for path in sorted(directory.glob("*.sql")):
        # Normalise line endings so a Windows checkout (CRLF) gives the same checksum.
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        migrations.append(Migration(path.stem, text, hashlib.sha256(text.encode()).hexdigest()))
    if not migrations:
        raise MigrationError(f"no *.sql migrations found in {directory}")
    return migrations


def applied_migrations(conn: psycopg.Connection) -> dict[str, str]:
    exists = conn.execute("SELECT to_regclass('ops.schema_migration') IS NOT NULL").fetchone()
    if not (exists and exists[0]):
        return {}
    rows = conn.execute("SELECT version, checksum FROM ops.schema_migration").fetchall()
    return dict(rows)


def migration_problems(conn: psycopg.Connection, migrations: list[Migration]) -> list[str]:
    """Differences between migration files and the database (empty = up to date)."""
    applied = applied_migrations(conn)
    known = {m.version for m in migrations}
    problems = [f"{m.version} not applied" for m in migrations if m.version not in applied]
    problems += [
        f"{m.version} was modified after it was applied (checksum mismatch)"
        for m in migrations
        if m.version in applied and applied[m.version] != m.checksum
    ]
    problems += [f"{v} is applied but has no migration file" for v in sorted(applied.keys() - known)]
    return problems


def apply_migrations(conn: psycopg.Connection, directory: Path) -> list[str]:
    """Apply pending migrations in order; returns the versions applied now."""
    migrations = discover_migrations(directory)
    conn.execute("SELECT pg_advisory_lock(%s)", [MIGRATION_LOCK_KEY])
    try:
        conn.execute(BOOTSTRAP_SQL)
        applied = applied_migrations(conn)
        modified = [m.version for m in migrations if m.version in applied and applied[m.version] != m.checksum]
        if modified:
            raise MigrationError(f"applied migrations were modified: {modified}; add a new migration instead")
        done = []
        for migration in migrations:
            if migration.version in applied:
                continue
            with conn.transaction():
                conn.execute(migration.sql)
                conn.execute(
                    "INSERT INTO ops.schema_migration (version, checksum) VALUES (%s, %s)",
                    [migration.version, migration.checksum],
                )
            done.append(migration.version)
        return done
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", [MIGRATION_LOCK_KEY])
