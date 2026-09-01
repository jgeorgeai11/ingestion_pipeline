"""Unit tests for ingpipe_lib.db (engine factory and extension preflight)."""

import logging
from unittest.mock import MagicMock

import pytest
from ingpipe_lib.db import engine_scope, get_engine, require_extensions

# The four variables get_engine reads.
POSTGRES_VARS = ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD")


def _set_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    """Set a complete POSTGRES_* environment, with optional overrides.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        **overrides: Variable values replacing the defaults.
    """
    values = {
        "POSTGRES_HOST": "dbhost.example",
        "POSTGRES_PORT": "5433",
        "POSTGRES_USER": "svc_user",
        "POSTGRES_PASSWORD": "plain",
    }
    values.update(overrides)
    for var, value in values.items():
        monkeypatch.setenv(var, value)


class TestGetEngine:
    """Tests for the shared engine factory."""

    def test_reserved_characters_in_password_round_trip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A password full of URL-reserved characters survives the URL build.

        Regression guard for the f-string URL defect: `URL.create`
        percent-encodes the credential, so host/port/database resolve
        correctly instead of being corrupted by the @ / : / / ? # characters.
        """
        password = "p@ss/w?o#r:d"
        _set_env(monkeypatch, POSTGRES_PASSWORD=password)

        engine = get_engine("target_db")
        try:
            assert engine.url.host == "dbhost.example"
            assert engine.url.port == 5433
            assert engine.url.database == "target_db"
            assert engine.url.username == "svc_user"
            # The URL object stores the raw password; rendering it re-encodes.
            assert engine.url.password == password
        finally:
            engine.dispose()

    @pytest.mark.parametrize("missing", POSTGRES_VARS)
    def test_missing_env_var_raises_naming_it(
        self, monkeypatch: pytest.MonkeyPatch, missing: str
    ) -> None:
        """Each missing POSTGRES_* variable raises ValueError naming it."""
        _set_env(monkeypatch)
        monkeypatch.delenv(missing)

        with pytest.raises(ValueError, match=missing):
            get_engine("target_db")

    def test_non_integer_port_raises_naming_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-integer POSTGRES_PORT raises ValueError naming the value."""
        _set_env(monkeypatch, POSTGRES_PORT="fivefourthreetwo")

        with pytest.raises(ValueError, match=r"POSTGRES_PORT.*fivefourthreetwo"):
            get_engine("target_db")

    def test_no_credential_value_is_logged(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Neither the success nor the failure paths log a credential value."""
        secret = "sup3r-secret-pw"
        _set_env(monkeypatch, POSTGRES_PASSWORD=secret)

        with caplog.at_level(logging.DEBUG):
            engine = get_engine("target_db")
            engine.dispose()
            # Failure path: missing variable logs an error, still no secret.
            monkeypatch.delenv("POSTGRES_HOST")
            with pytest.raises(ValueError):
                get_engine("target_db")

        for record in caplog.records:
            assert secret not in record.getMessage()

    def test_engine_scope_disposes_on_exit(
        self, monkeypatch: pytest.MonkeyPatch, mocker
    ) -> None:
        """engine_scope disposes the engine (and its pool) even on error."""
        _set_env(monkeypatch)

        with engine_scope("target_db") as engine:
            spy = mocker.spy(engine, "dispose")
        assert spy.call_count == 1

        # Dispose also runs when the body raises.
        with pytest.raises(RuntimeError):
            with engine_scope("target_db") as engine:
                spy = mocker.spy(engine, "dispose")
                raise RuntimeError("boom")
        assert spy.call_count == 1


def _mock_engine_with_extensions(installed: list[str], db_name: str) -> MagicMock:
    """Build a mock Engine whose pg_extension query returns ``installed``.

    Args:
        installed: Extension names the mocked database reports as installed.
        db_name: Database name exposed on ``engine.url.database``.

    Returns:
        A MagicMock standing in for a SQLAlchemy Engine.
    """
    engine = MagicMock()
    engine.url.database = db_name
    conn = MagicMock()
    conn.execute.return_value = [(name,) for name in installed]
    engine.connect.return_value.__enter__.return_value = conn
    engine.connect.return_value.__exit__.return_value = False
    return engine


class TestRequireExtensions:
    """Tests for the extension preflight."""

    def test_present_extension_passes(self) -> None:
        """All required extensions installed: no exception."""
        engine = _mock_engine_with_extensions(
            ["plpgsql", "ltree", "vector"], "ingestion_test"
        )
        require_extensions(engine, ["ltree", "vector"])  # should not raise

    def test_missing_extension_raises_with_name_and_database(self) -> None:
        """A missing extension raises, naming it, the database, and the fix."""
        engine = _mock_engine_with_extensions(["plpgsql", "ltree"], "ingestion_test")

        with pytest.raises(ValueError) as exc:
            require_extensions(engine, ["ltree", "vector"])

        message = str(exc.value)
        assert "vector" in message
        assert "ingestion_test" in message
        assert "CREATE EXTENSION IF NOT EXISTS vector" in message
        # The installed extension is not reported missing.
        assert "CREATE EXTENSION IF NOT EXISTS ltree" not in message

    def test_missing_extension_logged_at_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The failure is logged at ERROR before raising."""
        engine = _mock_engine_with_extensions([], "ingestion_test")

        with caplog.at_level(logging.ERROR):
            with pytest.raises(ValueError):
                require_extensions(engine, ["ltree"])

        assert any(
            "ltree" in record.getMessage() and record.levelno == logging.ERROR
            for record in caplog.records
        )
