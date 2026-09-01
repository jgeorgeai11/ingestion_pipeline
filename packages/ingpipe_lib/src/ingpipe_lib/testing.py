"""Shared test infrastructure for the workspace's unit tests.

Most of what lives here serves the DB-backed tests; ``assert_example_config_valid``
is the exception, needing neither a database nor a fixture.

Ships in ``ingpipe-lib`` (alongside the ``sql/`` provisioning scripts,
which provisions the database it targets) so the three engine packages'
conftests import ONE ``ephemeral_schema`` fixture instead of carrying copies
that drift, and so a future instance can reuse the same isolation pattern.

The fixture connects to the dedicated ``ingestion_test`` database — never a
corpus database — as the LOGIN-only ``ingestion_test_runner`` role, creates a
UUID-named throwaway schema, and drops it with CASCADE in teardown. An
unreachable database or absent ``.env.test`` SKIPS the DB-backed tests rather
than failing them, so the suite stays green without a database while
exercising real DDL when one is present.

This module is test-only: it imports ``pytest``, which the lib does not
declare as a runtime dependency (it rides in the workspace's dev group and
the lib's ``testing`` extra). Production modules must not import it.
"""

import tomllib
import uuid
from collections.abc import Callable, Iterator
from importlib.resources import files
from pathlib import Path

import pytest
from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from ingpipe_lib.db import get_engine

__all__ = [
    "assert_example_config_valid",
    "ephemeral_schema",
    "find_workspace_root",
    "load_test_env",
]


def find_workspace_root(start: Path) -> Path | None:
    """Walk up from ``start`` to the uv workspace root.

    The workspace root is the directory whose ``pyproject.toml`` carries the
    ``[tool.uv.workspace]`` table. Tests live inside an installed package's
    repo checkout, so the walk normally succeeds; ``None`` means the tests
    are running from an installed wheel with no workspace around them.

    Args:
        start: File or directory to start the walk from.

    Returns:
        The workspace root directory, or None if no workspace marker is found.
    """
    for candidate in [start, *start.parents]:
        marker = candidate / "pyproject.toml"
        if marker.is_file() and "[tool.uv.workspace]" in marker.read_text(encoding="utf-8"):
            return candidate
    return None


def load_test_env(start: Path) -> None:
    """Load the workspace-root ``.env.test`` for the DB-backed tests.

    The DB-backed tests run only against the dedicated ``ingestion_test``
    database (provisioned by this lib's ``sql/`` scripts),
    never a corpus database. Its credentials live in the workspace-root
    ``.env.test``, found by walking up from ``start`` so the path is
    CWD-independent; ``override=True`` so a stale ``POSTGRES_*`` exported in
    the shell cannot redirect the tests. A missing workspace root or
    ``.env.test`` is not an error here — ``get_engine`` then fails in the
    fixture and the DB-backed tests skip.

    Args:
        start: File or directory to start the workspace-root walk from
            (typically ``Path(__file__).resolve()`` in a conftest).
    """
    workspace_root = find_workspace_root(start)
    if workspace_root is not None:
        load_dotenv(workspace_root / ".env.test", override=True)


def assert_example_config_valid(import_package: str, validate: Callable[[dict], object]) -> dict:
    """Assert a package's shipped ``config/example.toml`` still satisfies its own validator.

    Every stage package ships an annotated ``config/example.toml`` documenting
    its config contract, and nothing ever executes it — so a required key added
    to the validator leaves a documented example that silently no longer works,
    and the cost lands on whoever copies it. This assertion closes that gap by
    running the shipped example through the real thing.

    ``validate`` must be the package's OWN ``validate_config`` (or a sibling
    validator such as ``validate_content_checks``), never a reimplementation of
    it: the point is to check the example against the code that will actually
    reject it at run time, and a second copy of the rules here would drift from
    the validator exactly the way the example drifts today.

    The example is resolved as a PACKAGE RESOURCE rather than by a path relative
    to the calling test file, so the lookup follows the import system. In the
    editable workspace that resolves to the source tree, proving only that the
    file sits at its canonical import location; it becomes load-bearing in the
    isolated-install check (MAINTAINING.packages.md section 1), where the package
    is installed alone from its wheel and ``files()`` resolves into
    site-packages, genuinely proving the file shipped. A test-file-relative read
    would pass wrongly there, because the repo checkout is present either way.

    Nothing is caught: a config the validator rejects surfaces the validator's
    own message and exception type, which are more informative than any generic
    assertion this helper could raise.

    Args:
        import_package: Import name of the package shipping the example (e.g.
            ``"ingpipe_acquisition"``), not the distribution name.
        validate: The package's own validator, called with the parsed config.
            Its return value is discarded — see Returns.

    Returns:
        The PARSED example config, so a caller can make further assertions about
        specific keys. The parsed dict rather than ``validate``'s result because
        the engine validators return ``None``; a caller needing normalized
        settings re-calls its validator on what it gets back.

    Raises:
        AssertionError: If the package ships no ``config/example.toml``.
        Exception: Whatever ``validate`` raises for a config it rejects,
            unchanged (a ``ValueError`` for every validator in this workspace).
    """
    resource = files(import_package) / "config" / "example.toml"
    if not resource.is_file():
        raise AssertionError(
            f"Package '{import_package}' ships no config/example.toml "
            f"(looked for {resource}). Every stage package documents its config "
            "contract with one; if this package genuinely has none, drop this "
            "assertion rather than leaving it failing."
        )

    config = tomllib.loads(resource.read_text(encoding="utf-8"))
    validate(config)
    return config


@pytest.fixture
def ephemeral_schema() -> Iterator[tuple[Engine, str]]:
    """Yield a real engine + a uniquely-named throwaway schema in ``ingestion_test``.

    Creates a fresh schema with a UUID-derived name in the dedicated test
    database, so parallel or aborted runs can never collide. The schema
    is dropped with CASCADE in teardown so it runs even if a test asserts/raises.
    Skips the test if the database cannot be reached, keeping the suite green
    without a DB while exercising real DDL when one is present.

    Yields:
        Tuple of (engine, schema_name).
    """
    try:
        engine = get_engine("ingestion_test")
    except ValueError as e:
        pytest.skip(f"Database env not configured: {e}")

    schema = f"eph_test_{uuid.uuid4().hex[:12]}"
    try:
        with engine.begin() as conn:
            conn.execute(text(f'create schema "{schema}"'))
    except SQLAlchemyError as e:
        engine.dispose()
        pytest.skip(f"Cannot connect to ingestion_test: {e}")

    try:
        yield engine, schema
    finally:
        try:
            with engine.begin() as conn:
                conn.execute(text(f'drop schema if exists "{schema}" cascade'))
        finally:
            engine.dispose()
