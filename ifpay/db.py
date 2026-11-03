"""PostgreSQL access: a connection pool on the IntraForge-provisioned schema, and a tiny forward-only migration runner."""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

_pool: ConnectionPool | None = None


def conninfo_for(*, host: str, port: int, dbname: str, role: str, password: str, schema: str) -> str:
    """The connection to the schema IntraForge provisioned for this Pay instance, as its own least-privilege role.
    Every unqualified name (tables, functions, the migration log) resolves inside that schema only."""
    if not _IDENT.match(schema) or not _IDENT.match(role):
        raise ValueError("IntraForge returned an invalid schema or role name.")
    return make_conninfo(host=host, port=port, dbname=dbname, user=role, password=password,
                         options=f"-c search_path={schema}", connect_timeout=10)


def open_pool(database_url: str, *, max_size: int = 10) -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            database_url,
            min_size=1,
            max_size=max_size,
            kwargs={"row_factory": dict_row, "autocommit": False},
            # Test each connection as it is handed out, so the server keeps working after PostgreSQL restarts.
            check=ConnectionPool.check_connection,
            open=True,
        )
    return _pool


def is_open() -> bool:
    return _pool is not None


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """A pooled connection; the block runs in one transaction, committed on success and rolled back on error."""
    if _pool is None:
        from .errors import DomainError

        raise DomainError("not_ready", "IntraForge Pay is starting, or is not installed in a shop yet.", status=503)
    with _pool.connection() as conn:
        yield conn


def migrate(database_url: str) -> list[str]:
    """Apply every migrations/*.sql not yet recorded in schema_migrations, each in its own transaction."""
    applied: list[str] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        # One migrator at a time (two server processes starting together must not both apply 0001).
        conn.execute("SELECT pg_advisory_lock(727374)")
        try:
            done = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.name in done:
                    continue
                with conn.transaction():
                    conn.execute(path.read_text(encoding="utf-8"))
                    conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
                applied.append(path.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(727374)")
    return applied
