"""Shared database plumbing for the workspace's entry-point scripts.

Provides the single engine factory every DB entry point uses (``get_engine``),
a disposing context manager for callers that would otherwise leak pooled
connections (``engine_scope``), and the extension preflight (``require_extensions``)
that backs the workspace's extension contract: **provisioning creates
extensions, the engine only verifies them**. Installing an extension is a
one-time provisioning act on the database requiring privileges an ingest run
must not hold, so a missing extension fails here — before any DDL — with an
actionable message naming the superuser command, rather than surfacing as an
opaque privilege error from the middle of a DDL transaction.

Before this module each package carried its own engine-factory copy; the five
copies had drifted (one used an f-string URL that corrupted credentials with
URL-reserved characters, others dropped the ERROR log or the port guard).
This is the one implementation: ``URL.create`` (percent-encodes credentials),
an ``int()`` port guard, and a logged ``ValueError`` with one standard message.
"""

import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine

from ingpipe_lib.logconfig import get_logger

__all__ = ["engine_scope", "get_engine", "require_extensions"]

logger = get_logger(__name__)


def get_engine(db_name: str) -> Engine:
    """Create a SQLAlchemy engine for the given PostgreSQL database.

    Reads ``POSTGRES_HOST``/``POSTGRES_PORT``/``POSTGRES_USER``/
    ``POSTGRES_PASSWORD`` from the environment and builds the URL with
    ``sqlalchemy.URL.create`` so credentials containing URL-reserved characters
    (``@``, ``:``, ``/``, ``?``, ``#``, ...) are percent-encoded safely instead
    of corrupting the connection string.

    Callers that create an engine per run (or per table) should dispose it when
    done — ``engine.dispose()`` in a ``finally``, or use :func:`engine_scope` —
    so pooled connections are not leaked for the process lifetime.

    Args:
        db_name: Name of the PostgreSQL database to connect to.

    Returns:
        A SQLAlchemy ``Engine`` bound to ``db_name``.

    Raises:
        ValueError: If any required ``POSTGRES_*`` environment variable is
            missing, or if ``POSTGRES_PORT`` is not an integer.
    """
    try:
        host = os.environ["POSTGRES_HOST"]
        port = os.environ["POSTGRES_PORT"]
        user = os.environ["POSTGRES_USER"]
        password = os.environ["POSTGRES_PASSWORD"]
    except KeyError as e:
        logger.error(f"Missing Postgres environment variable: {e}")
        raise ValueError(
            f"Missing Postgres environment variable: {e}. "
            "Ensure the --env-file supplies POSTGRES_HOST, POSTGRES_PORT, "
            "POSTGRES_USER, POSTGRES_PASSWORD"
        ) from e

    try:
        port_num = int(port)
    except ValueError as e:
        logger.error(f"POSTGRES_PORT is not an integer: {port!r}")
        raise ValueError(f"POSTGRES_PORT is not an integer: {port!r}") from e

    url = URL.create(
        "postgresql",
        username=user,
        password=password,
        host=host,
        port=port_num,
        database=db_name,
    )
    return create_engine(url)


@contextmanager
def engine_scope(db_name: str) -> Iterator[Engine]:
    """Yield an engine for ``db_name`` and dispose it (and its pool) on exit.

    Use for run-scoped connections so pooled connections are never leaked when
    the process outlives the run (e.g. a validator invoked from a long-lived
    caller, or a per-table engine inside a multi-table loop).

    Args:
        db_name: Name of the PostgreSQL database to connect to.

    Yields:
        A SQLAlchemy ``Engine`` bound to ``db_name``.

    Raises:
        ValueError: Propagated from :func:`get_engine` when the environment is
            incomplete or the port is not an integer.
    """
    engine = get_engine(db_name)
    try:
        yield engine
    finally:
        engine.dispose()


def require_extensions(engine: Engine, names: Sequence[str]) -> None:
    """Verify the named PostgreSQL extensions are installed, or fail actionably.

    The engine's extension contract: database provisioning installs extensions
    (a one-time superuser act), the engine only verifies them. This preflight
    runs before any DDL so a missing extension fails with a message naming the
    extension, the database checked, and the installing command — instead of a
    privilege error from the middle of a DDL transaction (pgvector, notably,
    is not a trusted extension, so even a database owner cannot create it).

    Args:
        engine: SQLAlchemy Engine connected to the target database.
        names: Extension names that must be present (e.g. ``["ltree"]``).

    Raises:
        ValueError: If any named extension is not installed in the engine's
            database. The message lists each missing extension and the
            superuser ``CREATE EXTENSION`` command that installs it.
    """
    with engine.connect() as conn:
        installed = {
            row[0]
            for row in conn.execute(text("select extname from pg_extension"))
        }

    missing = [name for name in names if name not in installed]
    if missing:
        db_name = engine.url.database
        install_commands = "; ".join(
            f"CREATE EXTENSION IF NOT EXISTS {name}" for name in missing
        )
        message = (
            f"Required PostgreSQL extension(s) not installed in database "
            f"{db_name!r}: {', '.join(missing)}. Installing an extension is a "
            "one-time provisioning act; run as a superuser against "
            f"{db_name!r}: {install_commands}"
        )
        logger.error(message)
        raise ValueError(message)

    logger.info(
        f"Extension preflight passed for database {engine.url.database!r}: "
        f"{', '.join(names)}"
    )
