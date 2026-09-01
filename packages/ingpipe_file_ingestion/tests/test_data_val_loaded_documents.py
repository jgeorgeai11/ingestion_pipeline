"""Unit tests for the data_val_loaded_documents entry point.

The SQL invariants run against a real PostgreSQL ephemeral schema: each check
must FAIL on violating data and PASS on conforming data. Violations that the
production DDL's own CHECK/FK constraints would block are staged in
look-alike tables (same columns, no constraints), because the validator's job
is to check the DATA independently of the DDL. main()'s error paths are
covered with mocks so no real credentials or database are needed there.
"""

import logging
import sys
from pathlib import Path

import pytest
from ingpipe_file_ingestion import ingest as ingest_module
from ingpipe_file_ingestion._utils import ensure_schema
from ingpipe_file_ingestion.data_validation import data_val_loaded_documents
from ingpipe_file_ingestion.data_validation.data_val_loaded_documents import (
    validate_loaded_documents,
)
from ingpipe_lib.testing import assert_example_config_valid
from pytest_mock import MockerFixture
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError


def test_main_without_env_file_flag_exits_usage_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A flag-less invocation is rejected by argparse (usage error, exit 2).

    --env-file is required so a run that forgets it fails loudly instead of
    silently connecting to whatever credentials the shell happened to hold.
    """
    # Arrange: argparse rejects before the config is ever opened, so the
    # config path only needs to exist as an argument.
    monkeypatch.setattr(
        sys,
        "argv",
        ["data_val_loaded_documents.py", "--config", str(tmp_path / "any.toml")],
    )

    # Act
    with pytest.raises(SystemExit) as exc:
        data_val_loaded_documents.main()

    # Assert: argparse's usage error names the missing flag.
    assert exc.value.code == 2
    assert "--env-file" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# validate_loaded_documents against a real database
# ---------------------------------------------------------------------------


def _real_tables(engine: Engine, schema: str) -> None:
    """Create the production document tables (real DDL, all constraints)."""
    ddl_path = Path(ingest_module.__file__).parent / "sql" / "schema.sql"
    ensure_schema(engine, schema, ddl_path)


def _lookalike_tables(engine: Engine, schema: str) -> None:
    """Create constraint-free look-alikes of the document tables.

    The validator must detect bad DATA independently of the DDL, so the
    violating fixtures are staged in tables without the CHECK/FK constraints
    that would otherwise block the inserts.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                f"create table {schema}.document ("
                "collection_path ltree primary key, title text, "
                "n_parsed_sections integer, source_binary_hash numeric(21,0), "
                "ingested_at timestamptz default now())"
            )
        )
        conn.execute(
            text(
                f"create table {schema}.document_content ("
                "collection_path ltree, sort_order integer, heading_text text, "
                "content_text text, word_count integer, page_start integer, "
                "page_end integer)"
            )
        )


def _insert_document(
    engine: Engine,
    schema: str,
    cp: str,
    n_sections: int,
    *,
    binary_hash: int = 42,
    with_content: bool = True,
) -> None:
    """Insert one document row and (optionally) its contiguous content rows."""
    with engine.begin() as conn:
        conn.execute(
            text(
                f"insert into {schema}.document "
                "(collection_path, title, n_parsed_sections, source_binary_hash) "
                "values (:cp, 'Title', :n, :h)"
            ),
            {"cp": cp, "n": n_sections, "h": binary_hash},
        )
        if with_content:
            for i in range(1, n_sections + 1):
                conn.execute(
                    text(
                        f"insert into {schema}.document_content "
                        "(collection_path, sort_order, heading_text, content_text, "
                        "word_count, page_start, page_end) "
                        "values (:cp, :so, 'H', 'body', 2, 1, 2)"
                    ),
                    {"cp": cp, "so": i},
                )


class TestValidateLoadedDocumentsRealDatabase:
    """Each invariant fails on violating data and passes on conforming data."""

    def test_conforming_data_passes_every_check(
        self, ephemeral_schema: tuple[Engine, str]
    ) -> None:
        """Documents matching every invariant produce zero failures."""
        engine, schema = ephemeral_schema
        _real_tables(engine, schema)
        _insert_document(engine, schema, "cms_iom.pub.a", 3)
        _insert_document(engine, schema, "cms_iom.pub.b", 1)

        failures = validate_loaded_documents(
            engine, schema, "document", "document_content",
            ["cms_iom.pub.a", "cms_iom.pub.b"],
        )

        assert failures == []

    def test_empty_schema_fails(self, ephemeral_schema: tuple[Engine, str]) -> None:
        """An empty document table is a single clear failure."""
        engine, schema = ephemeral_schema
        _real_tables(engine, schema)

        failures = validate_loaded_documents(
            engine, schema, "document", "document_content", ["cms_iom.pub.a"]
        )

        assert len(failures) == 1
        assert "empty" in failures[0]

    def test_expected_document_missing_fails(
        self, ephemeral_schema: tuple[Engine, str]
    ) -> None:
        """A config-expected collection_path absent from the table fails."""
        engine, schema = ephemeral_schema
        _real_tables(engine, schema)
        _insert_document(engine, schema, "cms_iom.pub.a", 1)

        failures = validate_loaded_documents(
            engine, schema, "document", "document_content",
            ["cms_iom.pub.a", "cms_iom.pub.missing"],
        )

        assert any("cms_iom.pub.missing" in f for f in failures)

    def test_section_count_mismatch_and_zero_content_fail(
        self, ephemeral_schema: tuple[Engine, str]
    ) -> None:
        """n_parsed_sections != content rows and zero-content both fail."""
        engine, schema = ephemeral_schema
        _real_tables(engine, schema)
        # Claims 3 sections, stores none.
        _insert_document(engine, schema, "cms_iom.pub.a", 3, with_content=False)

        failures = validate_loaded_documents(
            engine, schema, "document", "document_content", ["cms_iom.pub.a"]
        )

        assert any("n_parsed_sections" in f for f in failures)
        assert any("zero content rows" in f for f in failures)

    def test_orphan_content_rows_fail(
        self, ephemeral_schema: tuple[Engine, str]
    ) -> None:
        """Content rows without a parent document fail (FK checked as data)."""
        engine, schema = ephemeral_schema
        _lookalike_tables(engine, schema)
        _insert_document(engine, schema, "cms_iom.pub.a", 1)
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"insert into {schema}.document_content "
                    "(collection_path, sort_order, heading_text, content_text, word_count) "
                    "values ('cms_iom.pub.ghost', 1, 'H', 'body', 2)"
                )
            )

        failures = validate_loaded_documents(
            engine, schema, "document", "document_content", ["cms_iom.pub.a"]
        )

        assert any("orphan content row" in f for f in failures)

    def test_out_of_range_hash_fails(
        self, ephemeral_schema: tuple[Engine, str]
    ) -> None:
        """A source_binary_hash outside [0, 2^64) fails the range check."""
        engine, schema = ephemeral_schema
        _lookalike_tables(engine, schema)
        _insert_document(
            engine, schema, "cms_iom.pub.a", 1, binary_hash=2**64
        )

        failures = validate_loaded_documents(
            engine, schema, "document", "document_content", ["cms_iom.pub.a"]
        )

        assert any("source_binary_hash" in f for f in failures)

    def test_non_contiguous_sort_order_fails(
        self, ephemeral_schema: tuple[Engine, str]
    ) -> None:
        """sort_order with a gap (1, 3) fails the contiguity check."""
        engine, schema = ephemeral_schema
        _lookalike_tables(engine, schema)
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"insert into {schema}.document "
                    "(collection_path, title, n_parsed_sections, source_binary_hash) "
                    "values ('cms_iom.pub.a', 'T', 2, 1)"
                )
            )
            for so in (1, 3):
                conn.execute(
                    text(
                        f"insert into {schema}.document_content "
                        "(collection_path, sort_order, heading_text, content_text, word_count) "
                        "values ('cms_iom.pub.a', :so, 'H', 'body', 2)"
                    ),
                    {"so": so},
                )

        failures = validate_loaded_documents(
            engine, schema, "document", "document_content", ["cms_iom.pub.a"]
        )

        assert any("sort_order not 1-based contiguous" in f for f in failures)

    def test_negative_word_count_and_bad_page_order_fail(
        self, ephemeral_schema: tuple[Engine, str]
    ) -> None:
        """Negative word_count and page_start > page_end each fail."""
        engine, schema = ephemeral_schema
        _lookalike_tables(engine, schema)
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"insert into {schema}.document "
                    "(collection_path, title, n_parsed_sections, source_binary_hash) "
                    "values ('cms_iom.pub.a', 'T', 1, 1)"
                )
            )
            conn.execute(
                text(
                    f"insert into {schema}.document_content "
                    "(collection_path, sort_order, heading_text, content_text, "
                    "word_count, page_start, page_end) "
                    "values ('cms_iom.pub.a', 1, 'H', 'body', -1, 5, 2)"
                )
            )

        failures = validate_loaded_documents(
            engine, schema, "document", "document_content", ["cms_iom.pub.a"]
        )

        assert any("word_count" in f for f in failures)
        assert any("page_start > page_end" in f for f in failures)

    def test_unsafe_identifier_raises(
        self, ephemeral_schema: tuple[Engine, str]
    ) -> None:
        """An unsafe schema/table identifier raises before any SQL runs."""
        engine, _ = ephemeral_schema
        with pytest.raises(ValueError, match="db_schema"):
            validate_loaded_documents(
                engine, "bad-schema", "document", "document_content", ["a.b"]
            )


# ---------------------------------------------------------------------------
# main() paths (mock-based; no real database)
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, body: str) -> str:
    """Write a TOML config under tmp_path and return its path string."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(body, encoding="utf-8")
    return str(config_path)


def _valid_config_body() -> str:
    """A minimal ingest config the validator accepts (top-level db target)."""
    return (
        'db_name = "policy_db"\n'
        'db_schema = "cms_iom"\n'
        "[module]\n"
        'source_dir = "src"\n'
        "[[module.documents]]\n"
        'file = "a.pdf"\n'
        'title = "A"\n'
        'collection_path = "cms_iom.pub.a"\n'
        "[parse]\n"
        'parsed_dir = "parsed"\n'
        "[clean]\n"
        'cleaned_dir = "cleaned"\n'
    )


def _empty_env(tmp_path: Path) -> str:
    """Write an empty dotenv file for the required --env-file flag."""
    env_path = tmp_path / ".env.empty"
    env_path.write_text("", encoding="utf-8")
    return str(env_path)


def _run_main(mocker: MockerFixture, config: str, env_file: str) -> None:
    """Invoke main() with the given --config/--env-file and mocked logging."""
    mocker.patch(
        "ingpipe_file_ingestion.data_validation.data_val_loaded_documents.setup_entry_logging"
    )
    mocker.patch(
        "sys.argv",
        ["data_val_loaded_documents.py", "--config", config, "--env-file", env_file],
    )
    data_val_loaded_documents.main()


def test_the_shipped_example_config_still_satisfies_validate_config() -> None:
    """The annotated config/example.toml this package ships still validates.

    Nothing ever executes the example, so drift from ``validate_config`` is
    silent and permanent: a required key added to the validator leaves a
    documented example that no longer works, and the cost lands on whoever
    copies it. This test is the only thing that runs it.

    It sits beside the validator tests above deliberately -- the gate it checks
    against is the same one those cover, so a change to one is a change to both.
    """
    assert_example_config_valid("ingpipe_file_ingestion", ingest_module.validate_config)


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

    def test_missing_config_exits_one(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A missing config file exits 1."""
        with pytest.raises(SystemExit) as exc:
            _run_main(mocker, str(tmp_path / "absent.toml"), _empty_env(tmp_path))
        assert exc.value.code == 1

    def test_malformed_toml_exits_one(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """Malformed TOML exits 1."""
        config = _write_config(tmp_path, "not valid = toml ][")
        with pytest.raises(SystemExit) as exc:
            _run_main(mocker, config, _empty_env(tmp_path))
        assert exc.value.code == 1

    def test_missing_config_field_exits_one(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A config without [module] fails the shared validate_config gate."""
        config = _write_config(
            tmp_path, 'db_name = "x"\ndb_schema = "y"\n'
        )
        with pytest.raises(SystemExit) as exc:
            _run_main(mocker, config, _empty_env(tmp_path))
        assert exc.value.code == 1

    def test_no_valid_documents_exits_one(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A config whose documents all lack a valid collection_path exits 1."""
        config = _write_config(
            tmp_path,
            'db_name = "policy_db"\n'
            'db_schema = "cms_iom"\n'
            "[module]\n"
            'source_dir = "src"\n'
            "[[module.documents]]\n"
            'file = "a.pdf"\n'
            'title = "A"\n'
            'collection_path = "Not-Valid"\n'
            "[parse]\n"
            'parsed_dir = "parsed"\n'
            "[clean]\n"
            'cleaned_dir = "cleaned"\n',
        )
        with pytest.raises(SystemExit) as exc:
            _run_main(mocker, config, _empty_env(tmp_path))
        assert exc.value.code == 1

    def test_bad_postgres_env_exits_one(
        self, tmp_path: Path, mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing POSTGRES_* variable exits 1 via the engine factory."""
        for var in ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD"):
            monkeypatch.delenv(var, raising=False)
        config = _write_config(tmp_path, _valid_config_body())
        with pytest.raises(SystemExit) as exc:
            _run_main(mocker, config, _empty_env(tmp_path))
        assert exc.value.code == 1

    def test_sqlalchemy_error_exits_one_and_disposes_engine(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A DB error during validation exits 1 AND still disposes the engine.

        Pins the finally the module's docstring promises: no pooled
        connections are left open when validation raises.
        """
        config = _write_config(tmp_path, _valid_config_body())
        mock_engine = mocker.MagicMock()
        mocker.patch(
            "ingpipe_file_ingestion.data_validation.data_val_loaded_documents.get_engine",
            return_value=mock_engine,
        )
        mocker.patch(
            "ingpipe_file_ingestion.data_validation.data_val_loaded_documents"
            ".validate_loaded_documents",
            side_effect=SQLAlchemyError("db down"),
        )

        with pytest.raises(SystemExit) as exc:
            _run_main(mocker, config, _empty_env(tmp_path))

        assert exc.value.code == 1
        mock_engine.dispose.assert_called_once()

    def test_failures_exit_one_and_success_exits_zero(
        self, tmp_path: Path, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Validation failures exit 1 with each logged; a clean run returns."""
        config = _write_config(tmp_path, _valid_config_body())
        mock_engine = mocker.MagicMock()
        mocker.patch(
            "ingpipe_file_ingestion.data_validation.data_val_loaded_documents.get_engine",
            return_value=mock_engine,
        )
        mock_validate = mocker.patch(
            "ingpipe_file_ingestion.data_validation.data_val_loaded_documents"
            ".validate_loaded_documents",
            return_value=["FAIL: something"],
        )

        with caplog.at_level(logging.ERROR):
            with pytest.raises(SystemExit) as exc:
                _run_main(mocker, config, _empty_env(tmp_path))
        assert exc.value.code == 1
        assert any("FAIL: something" in r.message for r in caplog.records)
        mock_engine.dispose.assert_called_once()

        # Clean run: no SystemExit, engine still disposed.
        mock_validate.return_value = []
        mock_engine.dispose.reset_mock()
        _run_main(mocker, config, _empty_env(tmp_path))
        mock_engine.dispose.assert_called_once()
