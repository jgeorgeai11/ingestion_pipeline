"""Unit tests for the shared environment loader (ingpipe_lib.env)."""

import logging
import os
from pathlib import Path

import pytest
from ingpipe_lib.env import load_env

# The four variables every PostgreSQL entry point reads.
POSTGRES_VARS = ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD")

# Value written into the dotenv files these tests build, distinct from the
# ambient values so precedence is unambiguous.
FILE_HOST = "file-host.example.org"
SHELL_HOST = "shell-host.example.org"


def write_env_file(tmp_path: Path, **values: str) -> Path:
    """Write a dotenv file into `tmp_path` and return its path.

    Args:
        tmp_path: Directory to write the file into.
        **values: Variable name/value pairs to write, one per line.

    Returns:
        Path to the written dotenv file.
    """
    env_path = tmp_path / ".env.test"
    env_path.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + "\n",
        encoding="utf-8",
    )
    return env_path


def test_load_env_named_file_replaces_existing_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A named file outranks a variable already present in the environment."""
    # Arrange
    env_path = write_env_file(tmp_path, POSTGRES_HOST=FILE_HOST)
    monkeypatch.setenv("POSTGRES_HOST", SHELL_HOST)

    # Act
    load_env(env_path)

    # Assert
    assert os.environ["POSTGRES_HOST"] == FILE_HOST


def test_load_env_missing_path_raises_filenotfounderror(tmp_path: Path) -> None:
    """A named path that does not exist fails fast, naming the path."""
    # Arrange
    missing = tmp_path / "nope" / ".env.absent"

    # Act / Assert: the message must carry the path so the operator can see
    # the typo rather than silently connecting to the wrong database.
    with pytest.raises(FileNotFoundError) as exc_info:
        load_env(missing)

    assert str(missing) in str(exc_info.value)


def test_load_env_named_file_sets_all_postgres_vars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """All four POSTGRES_* variables come from the named file."""
    # Arrange: start from an environment with none of the four set.
    for var in POSTGRES_VARS:
        monkeypatch.delenv(var, raising=False)
    env_path = write_env_file(
        tmp_path,
        POSTGRES_HOST=FILE_HOST,
        POSTGRES_PORT="5433",
        POSTGRES_USER="file_user",
        POSTGRES_PASSWORD="file_password",
    )

    # Act
    load_env(env_path)

    # Assert
    assert [os.environ[var] for var in POSTGRES_VARS] == [
        FILE_HOST,
        "5433",
        "file_user",
        "file_password",
    ]


def test_load_env_logs_loaded_file_without_values(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The INFO record names the file and never leaks a variable value."""
    # Arrange
    for var in POSTGRES_VARS:
        monkeypatch.delenv(var, raising=False)
    secret = "super_secret_password"
    env_path = write_env_file(
        tmp_path,
        POSTGRES_HOST=FILE_HOST,
        POSTGRES_PORT="5433",
        POSTGRES_USER="file_user",
        POSTGRES_PASSWORD=secret,
    )

    # Act
    with caplog.at_level(logging.INFO):
        load_env(env_path)

    # Assert: the path is reported ...
    assert str(env_path) in caplog.text
    # ... and no record carries a credential value.
    for value in (secret, "file_user", FILE_HOST, "5433"):
        assert value not in caplog.text
