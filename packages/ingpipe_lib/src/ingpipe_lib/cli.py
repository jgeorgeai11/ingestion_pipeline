"""Shared entry-point preamble for the workspace's CLI scripts.

Every entry point used to hand-roll the same four things — the ``--config`` /
``--env-file`` argparse pair, the logging setup, the config-load-and-validate
sequence, and the failure-accumulation tail — and the six copies had drifted
(five inherited DEBUG-level logging and logged full SQLAlchemy/torch output,
log files were named after the script so validating two configs overwrote the
first log, and several exit paths left unterminated run blocks). This module
is the one implementation:

  - :func:`build_parser` — the canonical argument pair with one help text.
  - :func:`setup_entry_logging` — INFO-level logging to
    ``<instance>/logs/<subdir>/<config-stem>.jsonl`` (named from the CONFIG
    stem, not the script, so sequential runs with different configs keep
    separate logs). Builds on ``ingpipe_lib.paths.resolve_log_dir`` for the
    location anchoring.
  - :func:`run_scope` — the run-boundary separators; the closing separator is
    emitted in a ``finally``, so EVERY exit path (including ``sys.exit``)
    terminates its run block.
  - :func:`load_config` — env-file resolution plus config existence/TOML
    parsing, exiting 1 with a logged error on any failure.
  - :func:`finish_run` — the failure-accumulation tail (each failure at
    ERROR, a counted summary, exit 1 if non-empty).
"""

import argparse
import logging
import sys
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ingpipe_lib.env import load_env
from ingpipe_lib.logconfig import get_logger, setup_logging
from ingpipe_lib.paths import resolve_log_dir

__all__ = [
    "RUN_SEPARATOR",
    "build_parser",
    "finish_run",
    "load_config",
    "run_scope",
    "setup_entry_logging",
]

logger = get_logger(__name__)

# The run-boundary separator every entry point brackets its runs with.
RUN_SEPARATOR = "=" * 60


def build_parser(description: str, *, env_file: bool = True) -> argparse.ArgumentParser:
    """Build the canonical entry-point argument parser.

    Provides the ``--config`` argument every entry point takes and, for DB
    entry points, the required ``--env-file`` argument — with one canonical
    help text for each, so the six copies cannot drift.

    Args:
        description: The argparse program description.
        env_file: Whether the entry point connects to PostgreSQL and therefore
            requires ``--env-file``. Defaults to True.

    Returns:
        An ``argparse.ArgumentParser`` with the standard arguments added; the
        caller may add script-specific arguments (e.g. ``--overwrite``) before
        parsing.
    """
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--config", type=str, required=True, help="Path to TOML configuration file"
    )
    if env_file:
        parser.add_argument(
            "--env-file",
            type=str,
            required=True,
            help="Path to a dotenv file supplying the POSTGRES_* variables "
            "(instance convention: instances/<instance>/.env[.role])",
        )
    return parser


def setup_entry_logging(subdir: str, config_path: Path) -> None:
    """Configure INFO-level logging named from the config stem.

    Logs anchor to the config's instance root (``<instance>/logs/<subdir>/``)
    so an installed script writes to the instance regardless of the caller's
    working directory. The log file is named after the CONFIG stem — not the
    script — so validating two configs in sequence keeps two logs instead of
    the second overwriting the first. The level is INFO for every entry point:
    DEBUG would capture full SQLAlchemy/torch output in production runs.

    Call after argparse (so ``--help`` creates no log files) and before
    :func:`run_scope`.

    Args:
        subdir: The per-module log subdirectory (e.g. ``"ingpipe_file_ingestion"`` or
            ``"ingpipe_excel_ingestion/data_validation"``).
        config_path: The run's ``--config`` path; its stem names the log file
            and its instance root anchors the directory.
    """
    setup_logging(
        log_dir=resolve_log_dir(subdir, config_path),
        log_name=config_path.stem,
        level=logging.INFO,
    )


@contextmanager
def run_scope() -> Iterator[None]:
    """Bracket a run with separators, closing on EVERY exit path.

    The opening separator logs on entry; the closing one logs in a
    ``finally``, so ``sys.exit`` (a ``SystemExit`` propagating out of the
    body) and unexpected exceptions still terminate the run block — no more
    unterminated runs on early exits.

    Yields:
        None. The entry point's whole body runs inside this scope.
    """
    logger.info(RUN_SEPARATOR)
    try:
        yield
    finally:
        logger.info(RUN_SEPARATOR)


def load_config(config_path: Path, env_file: str | Path | None = None) -> dict:
    """Resolve credentials and load the TOML config, exiting 1 on any failure.

    Order matters: the env file loads first (inside the run scope, so the
    loader's record naming the environment source lands in this run's log),
    then the config's existence is checked, then the TOML is parsed. Each
    failure logs an error naming the offending path and exits 1.

    Args:
        config_path: The ``--config`` path to load.
        env_file: The ``--env-file`` path for DB entry points, or None for
            entry points that take no credentials.

    Returns:
        The parsed TOML config as a dict.

    Raises:
        SystemExit: With code 1 when the env file is missing, the config file
            does not exist, or the TOML is malformed.
    """
    if env_file is not None:
        try:
            load_env(env_file)
        except FileNotFoundError:
            # load_env already logged the missing path at ERROR.
            sys.exit(1)

    if not config_path.exists():
        logger.error(f"Config file not found: {config_path}")
        sys.exit(1)

    try:
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError) as e:
        logger.error(f"Failed to read config file {config_path}: {e}")
        sys.exit(1)


def finish_run(
    failures: list[str], *, success_message: str, failure_prefix: str
) -> None:
    """The failure-accumulation tail every entry point ends with.

    Logs the counted summary at ERROR, then each accumulated failure at
    ERROR, and exits 1 — or logs the success message at INFO and returns when
    there are no failures. One message shape for all entry points.

    Args:
        failures: The run's accumulated failure messages (already formatted).
        success_message: INFO message for a clean run
            (e.g. ``"OUTPUT VALIDATION PASSED: all checks passed"``).
        failure_prefix: ERROR summary prefix; the failure count is appended
            (e.g. ``"OUTPUT VALIDATION FAILED"``).

    Raises:
        SystemExit: With code 1 when ``failures`` is non-empty.
    """
    if failures:
        logger.error(f"{failure_prefix}: {len(failures)} failure(s)")
        for failure in failures:
            logger.error(failure)
        sys.exit(1)
    logger.info(success_message)
