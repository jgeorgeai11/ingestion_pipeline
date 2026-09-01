"""Unit tests for the data_val_excel_outputs entry point.

Both legs' SQL invariants run against a real PostgreSQL ephemeral schema:
each check must FAIL on violating data and PASS on conforming data.
Violations the production DDL would block are staged in look-alike tables
(same columns, no constraints), because the validator's job is to check the
DATA independently of the DDL. main()'s error and cleanup paths are covered
with mocks.
"""

import logging
import sys
from pathlib import Path

import pytest
from ingpipe_excel_ingestion.data_validation import data_val_excel_outputs
from ingpipe_excel_ingestion.data_validation.data_val_excel_outputs import (
    validate_content_leg,
    validate_structured_table,
)
from ingpipe_excel_ingestion.ingest_excel import ensure_consolidated_tables
from pytest_mock import MockerFixture
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError


def test_main_without_env_file_flag_exits_usage_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A flag-less invocation is rejected by argparse (usage error, exit 2)."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["data_val_excel_outputs.py", "--config", str(tmp_path / "any.toml")],
    )

    with pytest.raises(SystemExit) as exc:
        data_val_excel_outputs.main()

    assert exc.value.code == 2
    assert "--env-file" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Real-database fixtures
# ---------------------------------------------------------------------------


def _lookalike_tables(engine: Engine, schema: str) -> None:
    """Create constraint-free look-alikes of the sheet/sheet_content tables."""
    with engine.begin() as conn:
        conn.execute(
            text(
                f'create table "{schema}".sheet ('
                "collection_path ltree primary key, title text, "
                "n_rows integer, source_binary_hash numeric(21,0), "
                "structured_table text)"
            )
        )
        conn.execute(
            text(
                f'create table "{schema}".sheet_content ('
                "collection_path ltree, sort_order integer, row_text text, "
                "word_count integer)"
            )
        )


def _insert_sheet(
    engine: Engine,
    schema: str,
    cp: str,
    n_rows: int,
    *,
    binary_hash: int = 42,
    with_content: bool = True,
) -> None:
    """Insert one sheet row and (optionally) its contiguous content rows."""
    with engine.begin() as conn:
        conn.execute(
            text(
                f'insert into "{schema}".sheet '
                "(collection_path, title, n_rows, source_binary_hash) "
                "values (:cp, 'Title', :n, :h)"
            ),
            {"cp": cp, "n": n_rows, "h": binary_hash},
        )
        if with_content:
            for i in range(1, n_rows + 1):
                conn.execute(
                    text(
                        f'insert into "{schema}".sheet_content '
                        "(collection_path, sort_order, row_text, word_count) "
                        "values (:cp, :so, 'Code: A', 2)"
                    ),
                    {"cp": cp, "so": i},
                )


class TestValidateContentLegRealDatabase:
    """Embedding-leg invariants against a real database."""

    def test_conforming_data_passes(
        self, ephemeral_schema: tuple[Engine, str]
    ) -> None:
        """A sheet with contiguous content rows produces zero failures."""
        engine, schema = ephemeral_schema
        ensure_consolidated_tables(engine, schema)
        _insert_sheet(engine, schema, "wb.alpha", 2)

        failures = validate_content_leg(
            engine, schema, "sheet", "sheet_content", {"wb.alpha"}
        )

        assert failures == []

    def test_content_less_sheet_and_count_mismatch_fail(
        self, ephemeral_schema: tuple[Engine, str]
    ) -> None:
        """A sheet row without content rows fails both related checks."""
        engine, schema = ephemeral_schema
        _lookalike_tables(engine, schema)
        _insert_sheet(engine, schema, "wb.alpha", 2, with_content=False)

        failures = validate_content_leg(
            engine, schema, "sheet", "sheet_content", {"wb.alpha"}
        )

        assert any("no sheet_content rows" in f for f in failures)
        assert any("n_rows=2" in f for f in failures)

    def test_orphan_content_empty_row_text_and_bad_counts_fail(
        self, ephemeral_schema: tuple[Engine, str]
    ) -> None:
        """Orphan content, empty row_text, and negative word_count each fail."""
        engine, schema = ephemeral_schema
        _lookalike_tables(engine, schema)
        _insert_sheet(engine, schema, "wb.alpha", 1)
        with engine.begin() as conn:
            conn.execute(
                text(
                    f'insert into "{schema}".sheet_content '
                    "(collection_path, sort_order, row_text, word_count) "
                    "values ('wb.ghost', 1, '   ', -2)"
                )
            )

        failures = validate_content_leg(
            engine, schema, "sheet", "sheet_content", {"wb.alpha"}
        )

        assert any("reference a missing" in f for f in failures)
        assert any("empty row_text" in f for f in failures)
        assert any("word_count < 0" in f for f in failures)

    def test_out_of_range_hash_and_gapped_sort_order_fail(
        self, ephemeral_schema: tuple[Engine, str]
    ) -> None:
        """Hash outside [0, 2^64) and non-contiguous sort_order each fail."""
        engine, schema = ephemeral_schema
        _lookalike_tables(engine, schema)
        _insert_sheet(
            engine, schema, "wb.alpha", 2, binary_hash=2**64, with_content=False
        )
        with engine.begin() as conn:
            for so in (1, 3):
                conn.execute(
                    text(
                        f'insert into "{schema}".sheet_content '
                        "(collection_path, sort_order, row_text, word_count) "
                        "values ('wb.alpha', :so, 'Code: A', 2)"
                    ),
                    {"so": so},
                )

        failures = validate_content_leg(
            engine, schema, "sheet", "sheet_content", {"wb.alpha"}
        )

        assert any("source_binary_hash" in f for f in failures)
        assert any("sort_order not contiguous" in f for f in failures)

    def test_configured_sheet_without_sheet_row_fails(
        self, ephemeral_schema: tuple[Engine, str]
    ) -> None:
        """An expected collection_path with no sheet row fails."""
        engine, schema = ephemeral_schema
        ensure_consolidated_tables(engine, schema)
        _insert_sheet(engine, schema, "wb.alpha", 1)

        failures = validate_content_leg(
            engine, schema, "sheet", "sheet_content", {"wb.alpha", "wb.missing"}
        )

        assert any("wb.missing" in f and "no sheet row" in f for f in failures)


class TestValidateStructuredTableRealDatabase:
    """Structured-leg invariants against a real database."""

    def _structured(self, engine: Engine, schema: str) -> None:
        """Create a minimal structured table (no FK, for orphan staging)."""
        with engine.begin() as conn:
            conn.execute(
                text(
                    f'create table "{schema}".codes ('
                    "collection_path ltree, sort_order integer, col_code text)"
                )
            )

    def test_missing_table_fails(
        self, ephemeral_schema: tuple[Engine, str]
    ) -> None:
        """A structured table that does not exist is a single failure."""
        engine, schema = ephemeral_schema
        _lookalike_tables(engine, schema)

        failures = validate_structured_table(
            engine, schema, "sheet", "codes", {"wb.alpha"}
        )

        assert failures == [
            f"FAIL: structured table {schema}.codes does not exist"
        ]

    def test_empty_table_fails(self, ephemeral_schema: tuple[Engine, str]) -> None:
        """A structured table with zero rows fails."""
        engine, schema = ephemeral_schema
        _lookalike_tables(engine, schema)
        self._structured(engine, schema)

        failures = validate_structured_table(
            engine, schema, "sheet", "codes", {"wb.alpha"}
        )

        assert any("has 0 rows" in f for f in failures)

    def test_null_identity_orphan_and_missing_source_fail(
        self, ephemeral_schema: tuple[Engine, str]
    ) -> None:
        """Null identity columns, orphan FK, and a missing source each fail."""
        engine, schema = ephemeral_schema
        _lookalike_tables(engine, schema)
        self._structured(engine, schema)
        _insert_sheet(engine, schema, "wb.alpha", 1)
        with engine.begin() as conn:
            # Orphan (no sheet row) with a NULL sort_order.
            conn.execute(
                text(
                    f'insert into "{schema}".codes '
                    "(collection_path, sort_order, col_code) "
                    "values ('wb.ghost', null, 'X')"
                )
            )

        failures = validate_structured_table(
            engine, schema, "sheet", "codes", {"wb.alpha"}
        )

        assert any("null identity column" in f for f in failures)
        assert any("no matching sheet row" in f for f in failures)
        assert any("missing source" in f and "wb.alpha" in f for f in failures)

    def test_conforming_structured_table_passes(
        self, ephemeral_schema: tuple[Engine, str]
    ) -> None:
        """A populated structured table with matching sources passes."""
        engine, schema = ephemeral_schema
        _lookalike_tables(engine, schema)
        self._structured(engine, schema)
        _insert_sheet(engine, schema, "wb.alpha", 1)
        with engine.begin() as conn:
            conn.execute(
                text(
                    f'insert into "{schema}".codes '
                    "(collection_path, sort_order, col_code) "
                    "values ('wb.alpha', 1, 'X')"
                )
            )

        failures = validate_structured_table(
            engine, schema, "sheet", "codes", {"wb.alpha"}
        )

        assert failures == []


# ---------------------------------------------------------------------------
# main() paths (mock-based; no real database)
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, body: str) -> str:
    """Write a TOML config under tmp_path and return its path string."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(body, encoding="utf-8")
    return str(config_path)


def _valid_config_body() -> str:
    """A minimal excel config that passes the ingester's validate_config."""
    return (
        'source_dir = "data/input"\n'
        'db_name = "policy_db"\n'
        'db_schema = "qpp_cm"\n'
        '[files."wb.xlsx"]\n'
        'sheets = [\n  { sheet = "Alpha", table = "codes" },\n]\n'
    )


def _empty_env(tmp_path: Path) -> str:
    """Write an empty dotenv file for the required --env-file flag."""
    env_path = tmp_path / ".env.empty"
    env_path.write_text("", encoding="utf-8")
    return str(env_path)


def _run_main(mocker: MockerFixture, config: str, env_file: str) -> None:
    """Invoke main() with the given --config/--env-file and mocked logging."""
    mocker.patch(
        "ingpipe_excel_ingestion.data_validation.data_val_excel_outputs.setup_entry_logging"
    )
    mocker.patch(
        "sys.argv",
        ["data_val_excel_outputs.py", "--config", config, "--env-file", env_file],
    )
    data_val_excel_outputs.main()


class TestMainPaths:
    """main()'s error and cleanup paths."""

    def test_missing_env_file_exits_one(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A missing --env-file exits 1 before any DB work."""
        config = _write_config(tmp_path, _valid_config_body())
        with pytest.raises(SystemExit) as exc:
            _run_main(mocker, config, str(tmp_path / "no-such.env"))
        assert exc.value.code == 1

    def test_missing_config_and_malformed_toml_exit_one(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A missing config file and malformed TOML each exit 1."""
        with pytest.raises(SystemExit) as exc:
            _run_main(mocker, str(tmp_path / "absent.toml"), _empty_env(tmp_path))
        assert exc.value.code == 1

        config = _write_config(tmp_path, "not valid = toml ][")
        with pytest.raises(SystemExit) as exc:
            _run_main(mocker, config, _empty_env(tmp_path))
        assert exc.value.code == 1

    def test_invalid_config_shape_exits_one(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A config the INGESTER would reject is rejected here too (14.6)."""
        # overwrite as a quoted string fails the shared validate_config gate.
        config = _write_config(
            tmp_path, 'overwrite = "false"\n' + _valid_config_body()
        )
        with pytest.raises(SystemExit) as exc:
            _run_main(mocker, config, _empty_env(tmp_path))
        assert exc.value.code == 1

    def test_db_error_exits_one_and_disposes_engine(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A DB error during validation exits 1 AND disposes the engine."""
        config = _write_config(tmp_path, _valid_config_body())
        mock_engine = mocker.MagicMock()
        mocker.patch(
            "ingpipe_excel_ingestion.data_validation.data_val_excel_outputs.get_engine",
            return_value=mock_engine,
        )
        mocker.patch(
            "ingpipe_excel_ingestion.data_validation.data_val_excel_outputs"
            ".validate_content_leg",
            side_effect=SQLAlchemyError("db down"),
        )

        with pytest.raises(SystemExit) as exc:
            _run_main(mocker, config, _empty_env(tmp_path))

        assert exc.value.code == 1
        mock_engine.dispose.assert_called_once()

    def test_failures_exit_one_success_exits_zero(
        self, tmp_path: Path, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Accumulated failures exit 1 (each logged); a clean run returns."""
        config = _write_config(tmp_path, _valid_config_body())
        mock_engine = mocker.MagicMock()
        mocker.patch(
            "ingpipe_excel_ingestion.data_validation.data_val_excel_outputs.get_engine",
            return_value=mock_engine,
        )
        mock_content = mocker.patch(
            "ingpipe_excel_ingestion.data_validation.data_val_excel_outputs"
            ".validate_content_leg",
            return_value=["FAIL: content leg broke"],
        )
        mock_structured = mocker.patch(
            "ingpipe_excel_ingestion.data_validation.data_val_excel_outputs"
            ".validate_structured_table",
            return_value=[],
        )

        with caplog.at_level(logging.ERROR):
            with pytest.raises(SystemExit) as exc:
                _run_main(mocker, config, _empty_env(tmp_path))
        assert exc.value.code == 1
        assert any("content leg broke" in r.message for r in caplog.records)
        # The configured structured table was validated with its sources.
        assert mock_structured.call_args.args[3] == "codes"
        mock_engine.dispose.assert_called_once()

        # Clean run: both legs pass, no SystemExit, engine disposed again.
        mock_content.return_value = []
        mock_engine.dispose.reset_mock()
        _run_main(mocker, config, _empty_env(tmp_path))
        mock_engine.dispose.assert_called_once()


class TestCollectionPathPrefixRealDatabase:
    """The validator must re-derive prefixed paths exactly as the ingester does.

    This is the failure mode that looks identical to a real one: the validator
    independently re-derives every ``collection_path`` to check what the
    ingester stored, so a prefix threaded into the ingester alone would make
    the validator compute prefix-less paths, find none of them, and report
    every configured sheet missing.
    """

    @staticmethod
    def _prefixed_config(schema: str, *, prefix: str | None) -> str:
        """Build a config for the ephemeral schema, with or without a prefix."""
        prefix_line = f'collection_path_prefix = "{prefix}"\n' if prefix else ""
        return (
            'source_dir = "data/input"\n'
            'db_name = "ingestion_test"\n'
            f'db_schema = "{schema}"\n'
            f"{prefix_line}"
            '[files."wb.xlsx"]\n'
            'sheets = [\n  { sheet = "Alpha" },\n]\n'
        )

    def test_a_prefixed_config_finds_the_prefixed_rows(
        self, ephemeral_schema: tuple[Engine, str], tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A prefixed config validates rows stored under the prefixed path."""
        engine, schema = ephemeral_schema
        ensure_consolidated_tables(engine, schema)
        _insert_sheet(engine, schema, "qpp_cm.lists.wb.alpha", 2)
        config = _write_config(
            tmp_path, self._prefixed_config(schema, prefix="qpp_cm.lists")
        )

        _run_main(mocker, config, str(Path(__file__).resolve().parents[3] / ".env.test"))

    def test_dropping_the_prefix_reports_every_sheet_missing(
        self, ephemeral_schema: tuple[Engine, str], tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """Without the prefix the validator looks for paths that do not exist.

        The guard for decision 9: this is exactly what a validator that had not
        been taught about the prefix would report for a perfectly good corpus.
        """
        engine, schema = ephemeral_schema
        ensure_consolidated_tables(engine, schema)
        _insert_sheet(engine, schema, "qpp_cm.lists.wb.alpha", 2)
        config = _write_config(tmp_path, self._prefixed_config(schema, prefix=None))

        with pytest.raises(SystemExit) as exc:
            _run_main(
                mocker, config, str(Path(__file__).resolve().parents[3] / ".env.test")
            )

        assert exc.value.code == 1
