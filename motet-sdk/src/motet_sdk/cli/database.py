"""
Motet - Database CLI

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-16

Description:
    CLI commands for database operations and migrations.
    These operations are local-only and do not have API equivalents.

Dependencies:
    - click: CLI framework
    - psycopg: PostgreSQL database adapter

Usage:
    motet-cli database migrate-pgvector          # Create pgvector tables

Notes:
    - CLI-only operations (no API equivalent)
    - Requires database access
    - ``_normalize_pg_dsn`` is idempotent for already percent-encoded passwords
      (EC2/RDS URLs); it must not double-encode ``%XX`` sequences.
"""

from typing import Any, cast
from urllib.parse import quote, unquote, urlparse, urlunparse

import click

# Import shared logging configuration
from ._logging import logger


def _normalize_pg_dsn(dsn: str) -> str:
    """
    Percent-encode the password in a postgresql DSN so special chars don't break parsers.

    Idempotent for already-encoded DSNs (EC2/RDS): decode with ``unquote`` then
    encode once with ``quote``. Re-quoting the raw netloc password without decoding
    double-encodes ``%XX`` (e.g. ``%2A`` → ``%252A``) and causes RDS
    ``password authentication failed``.
    """
    parsed = urlparse(dsn)
    if not parsed.hostname or parsed.username is None or parsed.password is None:
        return dsn
    user = quote(unquote(parsed.username), safe="")
    encoded_password = quote(unquote(parsed.password), safe="")
    hostport = (
        f"{parsed.hostname}:{parsed.port}" if parsed.port else parsed.hostname
    )
    new_netloc = f"{user}:{encoded_password}@{hostport}"
    return urlunparse(
        (
            parsed.scheme,
            new_netloc,
            parsed.path or "",
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


@click.group("database")
def database_group() -> None:
    """Database operations."""
    pass


@database_group.command("migrate-pgvector")
@click.option("--dsn", default=None, help="Postgres DSN; defaults to MOTET_PGVECTOR_DSN env")
@click.option("--table", default=None, help="Table name; defaults to config value")
@click.option("--retries", default=6, help="Connection retries with backoff (default 6)")
@click.option("--retry-delay", default=10, type=int, help="Seconds between retries (default 10)")
def migrate_pgvector(
    dsn: str | None, table: str | None, retries: int, retry_delay: int
) -> None:
    """Create pgvector table and basic indexes if not present.
    Retries connection when DB is not ready (e.g. RDS on first EC2 boot).
    """
    import time

    from motet.core import Config

    # Logging already configured via ._logging import at module level
    cfg = Config()
    dsn = dsn or cfg.pgvector_dsn
    table = table or cfg.pgvector_table
    if not dsn:
        click.echo("PGVector DSN not configured", err=True)
        raise SystemExit(1)
    dsn = _normalize_pg_dsn(dsn)  # encode password special chars (e.g. +) for psycopg
    import psycopg

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with psycopg.connect(dsn) as conn, conn.cursor() as cur:
                # Ensure pgvector extension
                try:
                    cur.execute(cast(Any, "CREATE EXTENSION IF NOT EXISTS vector;"))
                except Exception:
                    pass
                cur.execute(
                    cast(
                        Any,
                        f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        id TEXT PRIMARY KEY,
                        content TEXT NOT NULL,
                        tags TEXT[],
                        metadata JSONB,
                        embedding VECTOR(384)
                    );
                    """,
                    )
                )
                # Optional index example
                try:
                    cur.execute(
                        cast(
                            Any,
                            f"CREATE INDEX IF NOT EXISTS {table}_tags_idx ON {table} USING GIN (tags)",
                        )
                    )
                except Exception:
                    pass
                conn.commit()
            click.echo(f"PGVector table '{table}' is ready")
            return
        except Exception as e:
            last_error = e
            logger.warning(
                "migrate_pgvector attempt failed (attempt %s/%s): %s",
                attempt,
                retries,
                str(e),
            )
            if attempt < retries:
                click.echo(
                    f"DB not ready (attempt {attempt}/{retries}), retrying in {retry_delay}s..."
                )
                time.sleep(retry_delay)

    click.echo(f"PGVector migration failed after {retries} attempts: {last_error}", err=True)
    raise SystemExit(1)

