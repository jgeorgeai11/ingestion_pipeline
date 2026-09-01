"""Shared fixtures for ingpipe_excel_ingestion unit tests.

The ``ephemeral_schema`` fixture (and the ``.env.test`` loading behind it) is
the single shared definition in ``ingpipe_lib.testing``, imported here so
pytest discovers it for this package's tests.
"""

from pathlib import Path

from ingpipe_lib.testing import ephemeral_schema, load_test_env

__all__ = ["ephemeral_schema"]

load_test_env(Path(__file__).resolve())
