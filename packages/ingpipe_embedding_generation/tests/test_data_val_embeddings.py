"""Unit tests for the data_val_embeddings entry point.

validate_embeddings runs against a real PostgreSQL ephemeral schema (vector
extension provisioned by ingestion_test): each check must FAIL on violating
data and PASS on conforming data, with a deterministic word-based token
counter standing in for the model tokenizer. main()'s error paths — including
the model-loading handler and the task-19 regressions (auto-detected PKs, a
missing source table failing cleanly, streamed chunk reads) — are covered
with mocks where no database is needed.
"""

import logging
import sys
from pathlib import Path

import pytest
from ingpipe_embedding_generation.data_validation import data_val_embeddings
from ingpipe_embedding_generation.data_validation.data_val_embeddings import (
    validate_embeddings,
)
from pytest_mock import MockerFixture
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

EMBED_DIM = 4


def _count_tokens(text_value: str) -> int:
    """Deterministic word-based token counter (words + 2 specials)."""
    return len(text_value.split()) + 2


def _vec(dim: int = EMBED_DIM) -> str:
    """A pgvector literal of the given dimension."""
    return "[" + ",".join(["0"] * dim) + "]"


def test_main_without_env_file_flag_exits_usage_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A flag-less invocation is rejected by argparse (usage error, exit 2)."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["data_val_embeddings.py", "--config", str(tmp_path / "any.toml")],
    )

    with pytest.raises(SystemExit) as exc:
        data_val_embeddings.main()

    assert exc.value.code == 2
    assert "--env-file" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# validate_embeddings against a real database
# ---------------------------------------------------------------------------


def _make_tables(
    engine: Engine, schema: str, *, with_tsv: bool = True, with_gin: bool = True
) -> None:
    """Create the source + embedding tables (mirroring the generator's DDL)."""
    tsv_col = (
        ", chunk_tsv tsvector generated always as "
        "(to_tsvector('english', chunk_text)) stored"
        if with_tsv
        else ""
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                f"create table {schema}.src ("
                "fname text not null, so integer not null, "
                "content_text text, primary key (fname, so))"
            )
        )
        conn.execute(
            text(
                f"create table {schema}.src_embedding ("
                "fname text not null, so integer not null, "
                "chunk_number integer not null, chunk_text text not null, "
                f"word_count integer not null, embedding vector({EMBED_DIM})"
                f"{tsv_col}, primary key (fname, so, chunk_number))"
            )
        )
        if with_gin and with_tsv:
            conn.execute(
                text(
                    f"create index idx_src_embedding_chunk_tsv on "
                    f"{schema}.src_embedding using gin (chunk_tsv)"
                )
            )


def _insert_source(engine: Engine, schema: str, fname: str, so: int) -> None:
    """Insert one source row."""
    with engine.begin() as conn:
        conn.execute(
            text(
                f"insert into {schema}.src values (:f, :s, 'body text')"
            ),
            {"f": fname, "s": so},
        )


def _insert_embedding(
    engine: Engine,
    schema: str,
    fname: str,
    so: int,
    *,
    chunk_text: str = "content_text: body text",
    word_count: int = 3,
    vector: str | None = "default",
) -> None:
    """Insert one embedding row (vector=None stores a NULL embedding)."""
    value = _vec() if vector == "default" else vector
    with engine.begin() as conn:
        conn.execute(
            text(
                f"insert into {schema}.src_embedding "
                "(fname, so, chunk_number, chunk_text, word_count, embedding) "
                "values (:f, :s, 1, :ct, :wc, cast(:v as vector))"
            ),
            {"f": fname, "s": so, "ct": chunk_text, "wc": word_count, "v": value},
        )


def _validate(engine: Engine, schema: str, **overrides: object) -> list[str]:
    """Run validate_embeddings with test defaults, applying overrides."""
    kwargs: dict = dict(
        engine=engine,
        db_schema=schema,
        source_table="src",
        embedding_table="src_embedding",
        pk_columns=["fname", "so"],
        max_tokens=50,
        max_seq_length=512,
        expected_dimension=EMBED_DIM,
        count_tokens=_count_tokens,
        source_filter=None,
    )
    kwargs.update(overrides)
    return validate_embeddings(**kwargs)


class TestValidateEmbeddingsRealDatabase:
    """Each invariant fails on violating data and passes on conforming data."""

    def test_conforming_data_passes_every_check(
        self, ephemeral_schema: tuple[Engine, str]
    ) -> None:
        """Fully embedded rows with correct vectors produce zero failures."""
        engine, schema = ephemeral_schema
        _make_tables(engine, schema)
        _insert_source(engine, schema, "a.pdf", 1)
        _insert_embedding(engine, schema, "a.pdf", 1)

        assert _validate(engine, schema) == []

    def test_missing_embedding_table_fails(
        self, ephemeral_schema: tuple[Engine, str]
    ) -> None:
        """A missing embedding table is a single clear failure."""
        engine, schema = ephemeral_schema
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"create table {schema}.src (fname text primary key)"
                )
            )

        failures = _validate(engine, schema, pk_columns=["fname"])

        assert len(failures) == 1
        assert "does not exist" in failures[0]

    def test_empty_embedding_table_fails(
        self, ephemeral_schema: tuple[Engine, str]
    ) -> None:
        """An empty embedding table is a single clear failure."""
        engine, schema = ephemeral_schema
        _make_tables(engine, schema)
        _insert_source(engine, schema, "a.pdf", 1)

        failures = _validate(engine, schema)

        assert len(failures) == 1
        assert "empty" in failures[0]

    def test_unembedded_source_rows_fail(
        self, ephemeral_schema: tuple[Engine, str]
    ) -> None:
        """A source row without any embedding row fails the accounting check."""
        engine, schema = ephemeral_schema
        _make_tables(engine, schema)
        _insert_source(engine, schema, "a.pdf", 1)
        _insert_source(engine, schema, "a.pdf", 2)
        _insert_embedding(engine, schema, "a.pdf", 1)

        failures = _validate(engine, schema)

        assert any("1/2 source row(s) have no embedding" in f for f in failures)

    def test_source_filter_scopes_the_accounting(
        self, ephemeral_schema: tuple[Engine, str]
    ) -> None:
        """An unembedded row OUTSIDE the source_filter does not fail."""
        engine, schema = ephemeral_schema
        _make_tables(engine, schema)
        _insert_source(engine, schema, "keep.pdf", 1)
        _insert_source(engine, schema, "skip.pdf", 1)
        _insert_embedding(engine, schema, "keep.pdf", 1)

        failures = _validate(engine, schema, source_filter={"fname": "keep%"})

        assert failures == []

    def test_null_embedding_and_wrong_dimension_fail(
        self, ephemeral_schema: tuple[Engine, str]
    ) -> None:
        """A NULL embedding fails the dimension check."""
        engine, schema = ephemeral_schema
        _make_tables(engine, schema)
        _insert_source(engine, schema, "a.pdf", 1)
        _insert_embedding(engine, schema, "a.pdf", 1, vector=None)

        failures = _validate(engine, schema)

        assert any("null embedding" in f for f in failures)

    def test_orphan_embedding_rows_fail(
        self, ephemeral_schema: tuple[Engine, str]
    ) -> None:
        """An embedding row without a parent source row fails."""
        engine, schema = ephemeral_schema
        _make_tables(engine, schema)
        _insert_source(engine, schema, "a.pdf", 1)
        _insert_embedding(engine, schema, "a.pdf", 1)
        _insert_embedding(engine, schema, "ghost.pdf", 9)

        failures = _validate(engine, schema)

        assert any("orphan embedding row" in f for f in failures)

    def test_missing_chunk_tsv_and_gin_index_fail(
        self, ephemeral_schema: tuple[Engine, str]
    ) -> None:
        """A table without the generated tsv column and GIN index fails both."""
        engine, schema = ephemeral_schema
        _make_tables(engine, schema, with_tsv=False, with_gin=False)
        _insert_source(engine, schema, "a.pdf", 1)
        _insert_embedding(engine, schema, "a.pdf", 1)

        failures = _validate(engine, schema)

        assert any("chunk_tsv generated column" in f for f in failures)
        assert any("GIN index" in f for f in failures)

    def test_token_budget_gates(
        self, ephemeral_schema: tuple[Engine, str], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Multi-word over-budget chunks FAIL; single-word only warns.

        With the word-based counter, a 10-word chunk against max_tokens=5 is a
        chunker regression (FAIL); an unsplittable single-word chunk over the
        budget is a warning, not a failure; anything over max_seq_length is
        the hard truncation gate.
        """
        engine, schema = ephemeral_schema
        _make_tables(engine, schema)
        _insert_source(engine, schema, "multi.pdf", 1)
        _insert_source(engine, schema, "single.pdf", 1)
        _insert_embedding(
            engine, schema, "multi.pdf", 1,
            chunk_text=" ".join(["word"] * 10), word_count=10,
        )
        _insert_embedding(
            engine, schema, "single.pdf", 1,
            chunk_text="unsplittable", word_count=1,
        )

        with caplog.at_level(logging.WARNING):
            failures = _validate(engine, schema, max_tokens=2, max_seq_length=11)

        # 10 + 2 = 12 tokens: over max_seq_length (11) AND over budget.
        assert any("max_seq_length" in f for f in failures)
        assert any("multi-word chunk(s)" in f for f in failures)
        # The single-word chunk (3 tokens > 2 budget) warned but did not fail.
        assert not any("1 single-word" in f for f in failures)
        assert any("single-word chunk(s) exceed" in r.message for r in caplog.records)

    def test_chunk_text_read_is_streamed(
        self, ephemeral_schema: tuple[Engine, str], mocker: MockerFixture
    ) -> None:
        """The chunk_text scan streams (yield_per) instead of fetchall().

        Regression pin for task 19.2: the validator must not re-materialize
        the corpus-sized chunk set the generator deliberately streams.
        """
        engine, schema = ephemeral_schema
        _make_tables(engine, schema)
        _insert_source(engine, schema, "a.pdf", 1)
        _insert_embedding(engine, schema, "a.pdf", 1)
        spy = mocker.spy(Connection, "execution_options")

        _validate(engine, schema)

        assert any(
            call.kwargs.get("yield_per") == 1000 for call in spy.call_args_list
        )

    def test_unsafe_identifier_raises(
        self, ephemeral_schema: tuple[Engine, str]
    ) -> None:
        """An unsafe identifier raises before any SQL runs."""
        engine, _ = ephemeral_schema
        with pytest.raises(ValueError, match="db_schema"):
            _validate(engine, "bad-schema")


# ---------------------------------------------------------------------------
# main() paths (mock-based; no real database)
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, body: str) -> str:
    """Write a TOML config under tmp_path and return its path string."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(body, encoding="utf-8")
    return str(config_path)


def _valid_config_body(extra_table_keys: str = "") -> str:
    """A minimal embedding config that passes the generator's validate_config."""
    return (
        'db_name = "policy_db"\n'
        'db_schema = "cms_iom"\n'
        'model_name = "stub-model"\n'
        "[tables.document_content]\n"
        'embed_columns = ["content_text"]\n'
        + extra_table_keys
    )


def _empty_env(tmp_path: Path) -> str:
    """Write an empty dotenv file for the required --env-file flag."""
    env_path = tmp_path / ".env.empty"
    env_path.write_text("", encoding="utf-8")
    return str(env_path)


def _mock_model(mocker: MockerFixture) -> None:
    """Patch the model loader with a stub carrying dimension and tokenizer."""
    model = mocker.MagicMock()
    model.get_sentence_embedding_dimension.return_value = EMBED_DIM
    model.max_seq_length = 512
    model.tokenizer = mocker.MagicMock(return_value={"input_ids": [0, 1]})
    mocker.patch(
        "ingpipe_embedding_generation.data_validation.data_val_embeddings"
        "._get_embedding_model",
        return_value=model,
    )


def _run_main(mocker: MockerFixture, config: str, env_file: str) -> None:
    """Invoke main() with the given --config/--env-file and mocked logging."""
    mocker.patch(
        "ingpipe_embedding_generation.data_validation.data_val_embeddings"
        ".setup_entry_logging"
    )
    mocker.patch(
        "sys.argv",
        ["data_val_embeddings.py", "--config", config, "--env-file", env_file],
    )
    data_val_embeddings.main()


class TestMainPaths:
    """main()'s error, regression, and cleanup paths."""

    def test_missing_env_file_and_missing_config_exit_one(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """Missing --env-file and missing config each exit 1."""
        config = _write_config(tmp_path, _valid_config_body())
        with pytest.raises(SystemExit) as exc:
            _run_main(mocker, config, str(tmp_path / "no-such.env"))
        assert exc.value.code == 1

        with pytest.raises(SystemExit) as exc:
            _run_main(mocker, str(tmp_path / "absent.toml"), _empty_env(tmp_path))
        assert exc.value.code == 1

    def test_invalid_config_shape_exits_one(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A config failing the generator's validate_config gate exits 1."""
        config = _write_config(tmp_path, 'db_name = "x"\n')  # missing fields
        with pytest.raises(SystemExit) as exc:
            _run_main(mocker, config, _empty_env(tmp_path))
        assert exc.value.code == 1

    def test_model_load_failure_exits_one(
        self, tmp_path: Path, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The (ValueError, OSError) model-loading handler exits 1 cleanly."""
        config = _write_config(tmp_path, _valid_config_body())
        mocker.patch(
            "ingpipe_embedding_generation.data_validation.data_val_embeddings"
            "._get_embedding_model",
            side_effect=OSError("no such model on the hub"),
        )

        with caplog.at_level(logging.ERROR):
            with pytest.raises(SystemExit) as exc:
                _run_main(mocker, config, _empty_env(tmp_path))

        assert exc.value.code == 1
        assert any("Failed to load embedding model" in r.message for r in caplog.records)

    def test_missing_source_table_fails_cleanly(
        self, tmp_path: Path, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A missing source table is a clean FAIL + exit 1, not a traceback.

        Regression pin for task 19.2: get_pk_constraint used to run unguarded
        and raise a raw NoSuchTableError.
        """
        config = _write_config(tmp_path, _valid_config_body())
        _mock_model(mocker)
        mock_engine = mocker.MagicMock()
        mocker.patch(
            "ingpipe_embedding_generation.data_validation.data_val_embeddings.get_engine",
            return_value=mock_engine,
        )
        mock_insp = mocker.MagicMock()
        mock_insp.has_table.return_value = False
        mocker.patch(
            "ingpipe_embedding_generation.data_validation.data_val_embeddings.inspect",
            return_value=mock_insp,
        )

        with caplog.at_level(logging.ERROR):
            with pytest.raises(SystemExit) as exc:
                _run_main(mocker, config, _empty_env(tmp_path))

        assert exc.value.code == 1
        assert any(
            "source table cms_iom.document_content does not exist" in r.message
            for r in caplog.records
        )
        mock_engine.dispose.assert_called_once()

    def test_obsolete_source_table_pks_ignored_with_warning(
        self, tmp_path: Path, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
    ) -> None:
        """source_table_pks is ignored; PKs are always auto-detected.

        Regression pin for task 19.1: the generator no longer accepts the
        key, so honoring it would join on columns the generator never used.
        """
        config = _write_config(
            tmp_path, _valid_config_body('source_table_pks = ["wrong_col"]\n')
        )
        _mock_model(mocker)
        mock_engine = mocker.MagicMock()
        mocker.patch(
            "ingpipe_embedding_generation.data_validation.data_val_embeddings.get_engine",
            return_value=mock_engine,
        )
        mock_insp = mocker.MagicMock()
        mock_insp.has_table.return_value = True
        mocker.patch(
            "ingpipe_embedding_generation.data_validation.data_val_embeddings.inspect",
            return_value=mock_insp,
        )
        mock_detect = mocker.patch(
            "ingpipe_embedding_generation.data_validation.data_val_embeddings"
            "._detect_primary_keys",
            return_value=["fname", "so"],
        )
        mock_validate = mocker.patch(
            "ingpipe_embedding_generation.data_validation.data_val_embeddings"
            ".validate_embeddings",
            return_value=[],
        )

        with caplog.at_level(logging.WARNING):
            _run_main(mocker, config, _empty_env(tmp_path))

        # The obsolete key was warned about and the AUTO-DETECTED PKs (not
        # the config's wrong_col) reached validate_embeddings.
        assert any("source_table_pks" in r.message for r in caplog.records)
        mock_detect.assert_called_once()
        assert mock_validate.call_args.kwargs["pk_columns"] == ["fname", "so"]
        mock_engine.dispose.assert_called_once()
