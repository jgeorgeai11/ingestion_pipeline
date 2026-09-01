"""Shared fixtures for ingpipe_embedding_generation unit tests.

The ``ephemeral_schema`` fixture (and the ``.env.test`` loading behind it) is
the single shared definition in ``ingpipe_lib.testing``, imported here so
pytest discovers it for this package's tests. DB-backed tests run against the
dedicated ``ingestion_test`` database (which provisioning equips with the
``vector`` extension, so the fixture's schemas can host vector columns and
HNSW indexes) and skip cleanly when no database is available.
"""

from pathlib import Path

from ingpipe_lib.testing import ephemeral_schema, load_test_env

__all__ = ["ephemeral_schema"]

load_test_env(Path(__file__).resolve())
