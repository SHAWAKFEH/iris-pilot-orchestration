"""Database connection helpers."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

import psycopg

from iris_pilot.settings import Settings

# Fixed advisory-lock keys. A session-level lock is released automatically
# when the connection closes, including when the process crashes.
RUN_LOCK_KEY = 4_951_000_001
MIGRATION_LOCK_KEY = 4_951_000_002


def connect(settings: Settings) -> psycopg.Connection:
    """Autocommit connection; stages open explicit transactions where needed."""
    return psycopg.connect(
        settings.database_url,
        autocommit=True,
        connect_timeout=settings.connect_timeout_s,
        application_name="iris-pilot",
    )


def try_advisory_lock(conn: psycopg.Connection, key: int) -> bool:
    row = conn.execute("SELECT pg_try_advisory_lock(%s)", [key]).fetchone()
    return bool(row and row[0])


def redact_url(url: str) -> str:
    """Hide the password in a database URL or key=value conninfo before logging it."""
    if "://" in url:
        parts = urlsplit(url)
        if parts.password:
            host = parts.netloc.rsplit("@", 1)[-1]
            return urlunsplit(parts._replace(netloc=f"{parts.username}:***@{host}"))
        return url
    return re.sub(r"(password\s*=\s*)\S+", r"\1***", url)
