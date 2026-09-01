"""Unit tests for generate_embeddings.py.

Tests config parsing, dynamic SQL generation, PK auto-detection,
identifier validation, embed text building, source table verification,
engine creation, embedding model caching and device selection, contextual
chunk headers, the resolved COMMENT ON table description, token-counter
wiring, the resilience branches (source-filter validation, overwrite,
no-rows, row-boundary batching), the streamed read and per-batch executemany
insert (one round trip per batch, whole rows kept in one flush, flushing
interleaved with the source stream), the main() entry point, and end-to-end
generation.
"""

import importlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest
from ingpipe_embedding_generation import generate_embeddings as generate_embeddings_module

# Imported at module level (collection time) rather than inside the tests:
# a test-time ``from ingpipe_embedding_generation._utils import ...`` could resolve to a same-named
# sibling module cached by a later-collected directory's suite.
from ingpipe_embedding_generation._utils import get_tokenizer, make_token_counter
from ingpipe_embedding_generation.generate_embeddings import (
    ConfigurationError,
    _build_embed_text,
    _build_source_filter_clause,
    _create_embedding_table,
    _detect_primary_keys,
    _resolve_pg_column_type,
    _validate_config_identifiers,
    _verify_source_table_exists,
    validate_config,
    validate_sql_identifier,
)
from ingpipe_lib.testing import assert_example_config_valid
from pytest_mock import MockType


class TestValidateSqlIdentifier:
    """Tests for the re-exported canonical validate_sql_identifier."""

    def test_validate_sql_identifier_accepts_safe_identifier(self) -> None:
        """A safe identifier passes through unchanged."""
        assert validate_sql_identifier("sheet_content", "table") == "sheet_content"

    def test_validate_sql_identifier_rejects_trailing_newline(self) -> None:
        """fullmatch (not match) rejects a trailing newline — the re-export fix."""
        with pytest.raises(ValueError, match="Unsafe SQL identifier"):
            validate_sql_identifier("sheet_content\n", "table")


class TestBuildEmbedText:
    """Tests for _build_embed_text."""

    def test_build_embed_text_single_column_returns_prefixed_value(self) -> None:
        """Single embed column returns its prefixed value."""
        row = {"content_text": "Hello world"}
        result = _build_embed_text(row, ["content_text"])
        assert result == "content_text: Hello world"

    def test_build_embed_text_multiple_columns_joined_with_newline(self) -> None:
        """Multiple embed columns are joined with a newline and column prefixes."""
        row = {"heading_text": "Chapter 1", "content_text": "Body text here"}
        result = _build_embed_text(row, ["heading_text", "content_text"])
        assert result == "heading_text: Chapter 1\ncontent_text: Body text here"

    def test_build_embed_text_none_value_renders_empty(self) -> None:
        """A None value renders as an empty value, keeping the layout stable.

        Matches ingest_excel.build_row_text: a stable field layout across rows
        means the embedding model sees a consistent schema, rather than a
        per-row layout that varies with which columns happen to be NULL.
        """
        row = {"heading_text": None, "content_text": "Body text"}
        result = _build_embed_text(row, ["heading_text", "content_text"])
        assert result == "heading_text: \ncontent_text: Body text"

    def test_build_embed_text_missing_column_treated_as_empty(self) -> None:
        """Missing column key is treated as empty string with prefix."""
        row = {"content_text": "Body text"}
        result = _build_embed_text(row, ["heading_text", "content_text"])
        assert result == "heading_text: \ncontent_text: Body text"

    def test_build_embed_text_cms_iom_columns(self) -> None:
        """Verify concatenation with cms_iom-style columns."""
        row = {"heading_text": "General Information", "content_text": "Medicare overview"}
        result = _build_embed_text(row, ["heading_text", "content_text"])
        assert result == "heading_text: General Information\ncontent_text: Medicare overview"


class TestBuildSourceFilterClause:
    """Tests for _build_source_filter_clause."""

    def test_build_source_filter_clause_none_returns_empty(self) -> None:
        """None source_filter returns empty clause and params."""
        clause, params = _build_source_filter_clause(None)
        assert clause == ""
        assert params == {}

    def test_build_source_filter_clause_empty_dict_returns_empty(self) -> None:
        """Empty dict returns empty clause and params."""
        clause, params = _build_source_filter_clause({})
        assert clause == ""
        assert params == {}

    def test_build_source_filter_clause_single_column_single_value(self) -> None:
        """Single column with a string value produces a simple LIKE clause."""
        clause, params = _build_source_filter_clause({"module_name": "Pub. 100-01%"})
        assert clause == "module_name::text like :sf_module_name_0"
        assert params == {"sf_module_name_0": "Pub. 100-01%"}

    def test_build_source_filter_clause_single_column_multiple_values_ored(self) -> None:
        """Multiple values for one column are ORed together."""
        clause, params = _build_source_filter_clause({"filename": ["ge101%", "ge102%"]})
        assert clause == "(filename::text like :sf_filename_0 or filename::text like :sf_filename_1)"
        assert params == {"sf_filename_0": "ge101%", "sf_filename_1": "ge102%"}

    def test_build_source_filter_clause_multiple_columns_anded(self) -> None:
        """Multiple columns are ANDed together."""
        clause, params = _build_source_filter_clause({
            "filename": ["ge101%", "ge102%"],
            "module_name": "Pub. 100-01%",
        })
        assert "(filename::text like :sf_filename_0 or filename::text like :sf_filename_1)" in clause
        assert "module_name::text like :sf_module_name_0" in clause
        assert " and " in clause
        assert params["sf_filename_0"] == "ge101%"
        assert params["sf_filename_1"] == "ge102%"
        assert params["sf_module_name_0"] == "Pub. 100-01%"

    def test_build_source_filter_clause_invalid_column_name_raises(self) -> None:
        """Unsafe SQL identifier in column name raises ValueError."""
        with pytest.raises(ValueError, match="source_filter column"):
            _build_source_filter_clause({"bad; column": "value%"})

    def test_build_source_filter_clause_single_element_list_no_parens(self) -> None:
        """A list with one element produces no surrounding parentheses."""
        clause, _params = _build_source_filter_clause({"filename": ["ge101%"]})
        assert clause == "filename::text like :sf_filename_0"
        assert "(" not in clause

    def test_build_source_filter_clause_with_table_alias(self) -> None:
        """Table alias is prefixed to column names when provided."""
        clause, params = _build_source_filter_clause(
            {"module_name": "Pub. 100-01%"}, table_alias="src"
        )
        assert clause == "src.module_name::text like :sf_module_name_0"
        assert params == {"sf_module_name_0": "Pub. 100-01%"}

    def test_build_source_filter_clause_alias_with_multiple_columns(self) -> None:
        """Table alias is applied to all column names in a multi-column filter."""
        clause, _params = _build_source_filter_clause(
            {"filename": ["ge101%"], "module_name": "Pub%"}, table_alias="src"
        )
        assert "src.filename::text like" in clause
        assert "src.module_name::text like" in clause


class TestValidateConfigIdentifiers:
    """Tests for _validate_config_identifiers."""

    def test_validate_config_identifiers_valid_identifiers_pass(self) -> None:
        """All valid SQL identifiers pass without error."""
        _validate_config_identifiers(
            db_schema="public",
            source_table="sections",
            embedding_table="sections_embedding",
            embed_columns=["heading_text", "content_text"],
            pk_columns=["filename", "sort_order"],
        )

    def test_validate_config_identifiers_invalid_source_table_raises(self) -> None:
        """Source table with unsafe characters raises ValueError."""
        with pytest.raises(ValueError, match="source_table"):
            _validate_config_identifiers(
                db_schema="public",
                source_table="sections; DROP TABLE",
                embedding_table="sections_embedding",
                embed_columns=["content_text"],
                pk_columns=["id"],
            )

    def test_validate_config_identifiers_invalid_embed_column_raises(self) -> None:
        """Embed column with uppercase raises ValueError."""
        with pytest.raises(ValueError, match="embed_column"):
            _validate_config_identifiers(
                db_schema="public",
                source_table="sections",
                embedding_table="sections_embedding",
                embed_columns=["Content_Text"],
                pk_columns=["id"],
            )

    def test_validate_config_identifiers_invalid_pk_column_raises(self) -> None:
        """PK column with spaces raises ValueError."""
        with pytest.raises(ValueError, match="pk_column"):
            _validate_config_identifiers(
                db_schema="public",
                source_table="sections",
                embedding_table="sections_embedding",
                embed_columns=["content_text"],
                pk_columns=["file name"],
            )

    def test_validate_config_identifiers_invalid_header_column_raises(self) -> None:
        """Header column with unsafe characters raises ValueError.

        Header columns are interpolated into the SELECT column list, so they
        are validated like embed and PK columns.
        """
        with pytest.raises(ValueError, match="header_column"):
            _validate_config_identifiers(
                db_schema="public",
                source_table="sections",
                embedding_table="sections_embedding",
                embed_columns=["content_text"],
                pk_columns=["id"],
                header_columns=["heading_text; DROP TABLE"],
            )

    def test_validate_config_identifiers_invalid_schema_raises(self) -> None:
        """Schema with special characters raises ValueError."""
        with pytest.raises(ValueError, match="db_schema"):
            _validate_config_identifiers(
                db_schema="my-schema",
                source_table="sections",
                embedding_table="sections_embedding",
                embed_columns=["content_text"],
                pk_columns=["id"],
            )


def test_the_shipped_example_config_still_satisfies_validate_config() -> None:
    """The annotated config/example.toml this package ships still validates.

    Nothing ever executes the example, so drift from ``validate_config`` is
    silent and permanent: a required key added to the validator leaves a
    documented example that no longer works, and the cost lands on whoever
    copies it. This test is the only thing that runs it.

    ``validate_config`` is the gate both this module and ``data_val_embeddings``
    call, so one check covers the example against both entry points.
    """
    assert_example_config_valid("ingpipe_embedding_generation", validate_config)


class TestDetectPrimaryKeys:
    """Tests for _detect_primary_keys."""

    def test_detect_primary_keys_auto_detects_pks(self, mocker) -> None:
        """Auto-detects PKs from SQLAlchemy inspect."""
        mock_inspect = mocker.patch("ingpipe_embedding_generation.generate_embeddings.inspect")
        mock_insp = mocker.MagicMock()
        mock_insp.get_pk_constraint.return_value = {
            "constrained_columns": ["filename", "sort_order"]
        }
        mock_inspect.return_value = mock_insp

        engine = mocker.MagicMock()
        result = _detect_primary_keys(engine, "public", "sections")

        assert result == ["filename", "sort_order"]
        mock_insp.get_pk_constraint.assert_called_once_with("sections", schema="public")

    def test_detect_primary_keys_no_pks_raises_error(self, mocker) -> None:
        """Raises ConfigurationError when no PKs found."""
        mock_inspect = mocker.patch("ingpipe_embedding_generation.generate_embeddings.inspect")
        mock_insp = mocker.MagicMock()
        mock_insp.get_pk_constraint.return_value = {"constrained_columns": []}
        mock_inspect.return_value = mock_insp

        engine = mocker.MagicMock()
        with pytest.raises(ConfigurationError, match="No primary key found"):
            _detect_primary_keys(engine, "public", "sections")

    def test_detect_primary_keys_missing_constraint_key_raises_error(self, mocker) -> None:
        """Raises ConfigurationError when constraint dict lacks expected key."""
        mock_inspect = mocker.patch("ingpipe_embedding_generation.generate_embeddings.inspect")
        mock_insp = mocker.MagicMock()
        mock_insp.get_pk_constraint.return_value = {}
        mock_inspect.return_value = mock_insp

        engine = mocker.MagicMock()
        with pytest.raises(ConfigurationError, match="No primary key found"):
            _detect_primary_keys(engine, "public", "sections")


class TestVerifySourceTableExists:
    """Tests for _verify_source_table_exists."""

    def test_verify_source_table_exists_table_found(self, mocker) -> None:
        """No error raised when source table exists."""
        mock_inspect = mocker.patch("ingpipe_embedding_generation.generate_embeddings.inspect")
        mock_insp = mocker.MagicMock()
        mock_insp.has_table.return_value = True
        mock_inspect.return_value = mock_insp

        engine = mocker.MagicMock()
        _verify_source_table_exists(engine, "public", "sections")

        mock_insp.has_table.assert_called_once_with("sections", schema="public")

    def test_verify_source_table_exists_table_not_found_raises(self, mocker) -> None:
        """Raises ConfigurationError when source table does not exist."""
        mock_inspect = mocker.patch("ingpipe_embedding_generation.generate_embeddings.inspect")
        mock_insp = mocker.MagicMock()
        mock_insp.has_table.return_value = False
        mock_inspect.return_value = mock_insp

        engine = mocker.MagicMock()
        with pytest.raises(ConfigurationError, match="does not exist"):
            _verify_source_table_exists(engine, "public", "sections")


class TestCreateEmbeddingTable:
    """Tests for _create_embedding_table."""

    def test_create_embedding_table_single_pk(self, mocker) -> None:
        """DDL is executed with correct structure for single PK."""
        mock_inspect = mocker.patch("ingpipe_embedding_generation.generate_embeddings.inspect")
        mock_insp = mocker.MagicMock()
        mock_insp.get_columns.return_value = [
            {"name": "id", "type": mocker.MagicMock(compile=lambda dialect: "integer")}
        ]
        mock_inspect.return_value = mock_insp

        engine = mocker.MagicMock()
        mock_conn = mocker.MagicMock()
        engine.begin.return_value.__enter__ = mocker.MagicMock(return_value=mock_conn)
        engine.begin.return_value.__exit__ = mocker.MagicMock(return_value=False)

        _create_embedding_table(
            engine, "public", "sections", "sections_embedding", ["id"], 1024,
            "Chunks of public.sections",
        )

        # DDL + COMMENT ON, both in the same transaction.
        assert mock_conn.execute.call_count == 2
        executed_sql = str(mock_conn.execute.call_args_list[0][0][0].text)
        assert "create table if not exists public.sections_embedding" in executed_sql
        assert "primary key (id, chunk_number)" in executed_sql
        assert "foreign key (id) references public.sections (id)" in executed_sql
        # Hybrid-ready FTS: generated tsvector column + GIN index in the same DDL.
        assert (
            "chunk_tsv tsvector generated always as (to_tsvector('english', chunk_text)) stored"
            in executed_sql
        )
        assert "idx_sections_embedding_chunk_tsv" in executed_sql
        assert "using gin (chunk_tsv)" in executed_sql

    def test_create_embedding_table_composite_pk(self, mocker) -> None:
        """DDL is executed with correct structure for composite PK."""
        mock_inspect = mocker.patch("ingpipe_embedding_generation.generate_embeddings.inspect")
        mock_insp = mocker.MagicMock()

        # Create mock column types that have a compile method
        mock_varchar = mocker.MagicMock()
        mock_varchar.compile.return_value = "varchar(255)"
        mock_int = mocker.MagicMock()
        mock_int.compile.return_value = "integer"

        mock_insp.get_columns.return_value = [
            {"name": "filename", "type": mock_varchar},
            {"name": "sort_order", "type": mock_int},
        ]
        mock_inspect.return_value = mock_insp

        engine = mocker.MagicMock()
        mock_conn = mocker.MagicMock()
        engine.begin.return_value.__enter__ = mocker.MagicMock(return_value=mock_conn)
        engine.begin.return_value.__exit__ = mocker.MagicMock(return_value=False)

        _create_embedding_table(
            engine, "public", "sections", "sections_embedding",
            ["filename", "sort_order"], 768, "Chunks of public.sections",
        )

        assert mock_conn.execute.call_count == 2
        executed_sql = str(mock_conn.execute.call_args_list[0][0][0].text)
        assert "primary key (filename, sort_order, chunk_number)" in executed_sql
        assert "foreign key (filename, sort_order)" in executed_sql

    def test_create_embedding_table_nulltype_pk_resolves_pg_type(self, mocker) -> None:
        """A NullType-reflected PK (e.g. ltree) falls back to the canonical PG type."""
        from sqlalchemy.types import NullType

        mock_inspect = mocker.patch("ingpipe_embedding_generation.generate_embeddings.inspect")
        mock_insp = mocker.MagicMock()
        mock_insp.get_columns.return_value = [
            {"name": "collection_path", "type": NullType()},
        ]
        mock_inspect.return_value = mock_insp

        # The format_type fallback resolves the unrecognized type to "ltree".
        mocker.patch(
            "ingpipe_embedding_generation.generate_embeddings._resolve_pg_column_type",
            return_value="ltree",
        )

        engine = mocker.MagicMock()
        mock_conn = mocker.MagicMock()
        engine.begin.return_value.__enter__ = mocker.MagicMock(return_value=mock_conn)
        engine.begin.return_value.__exit__ = mocker.MagicMock(return_value=False)

        _create_embedding_table(
            engine, "cms_iom", "document_content", "document_content_embedding",
            ["collection_path"], 1024, "Chunks of cms_iom.document_content",
        )

        assert mock_conn.execute.call_count == 2
        executed_sql = str(mock_conn.execute.call_args_list[0][0][0].text)
        assert "collection_path ltree not null" in executed_sql
        assert "primary key (collection_path, chunk_number)" in executed_sql

    def test_create_embedding_table_applies_comment_with_bound_text(
        self, mocker
    ) -> None:
        """The COMMENT ON runs after the DDL with the text as a bound parameter.

        The single quote in the text must survive intact in the bound params —
        the comment is data, not a spliced SQL literal.
        """
        mock_inspect = mocker.patch("ingpipe_embedding_generation.generate_embeddings.inspect")
        mock_insp = mocker.MagicMock()
        mock_insp.get_columns.return_value = [
            {"name": "id", "type": mocker.MagicMock(compile=lambda dialect: "integer")}
        ]
        mock_inspect.return_value = mock_insp

        engine = mocker.MagicMock()
        mock_conn = mocker.MagicMock()
        engine.begin.return_value.__enter__ = mocker.MagicMock(return_value=mock_conn)
        engine.begin.return_value.__exit__ = mocker.MagicMock(return_value=False)

        comment = "It's the sections' hybrid-retrieval chunk table"
        _create_embedding_table(
            engine, "public", "sections", "sections_embedding", ["id"], 1024, comment
        )

        comment_call = mock_conn.execute.call_args_list[1]
        assert (
            str(comment_call[0][0].text)
            == "comment on table public.sections_embedding is :comment_text"
        )
        assert comment_call[0][1] == {"comment_text": comment}


class TestResolvePgColumnType:
    """Tests for _resolve_pg_column_type."""

    def test_resolve_pg_column_type_returns_canonical_type(self, mocker) -> None:
        """Resolves the canonical PG type via format_type with the bound params."""
        engine = mocker.MagicMock()
        mock_conn = mocker.MagicMock()
        engine.connect.return_value.__enter__ = mocker.MagicMock(return_value=mock_conn)
        engine.connect.return_value.__exit__ = mocker.MagicMock(return_value=False)
        mock_conn.execute.return_value.scalar_one.return_value = "ltree"

        result = _resolve_pg_column_type(
            engine, "cms_iom", "document_content", "collection_path"
        )

        assert result == "ltree"
        bound_params = mock_conn.execute.call_args[0][1]
        assert bound_params == {
            "schema": "cms_iom",
            "tbl": "document_content",
            "col": "collection_path",
        }
        executed_sql = str(mock_conn.execute.call_args[0][0].text)
        assert "format_type(a.atttypid, a.atttypmod)" in executed_sql


class TestGetEmbeddingModel:
    """Tests for _get_embedding_model."""

    def test_get_embedding_model_loads_and_caches(self, mocker, monkeypatch) -> None:
        """Model is loaded on the MPS device on first call, then cached."""
        from ingpipe_embedding_generation import generate_embeddings

        # Reset the module-level cache via monkeypatch so the real globals are
        # restored after the test (a MagicMock left in _model would otherwise
        # stay installed as the process-wide model cache).
        monkeypatch.setattr(generate_embeddings, "_model", None)
        monkeypatch.setattr(generate_embeddings, "_model_name", None)

        mock_st = mocker.patch(
            "ingpipe_embedding_generation.generate_embeddings.SentenceTransformer"
        )
        mock_torch = mocker.patch("ingpipe_embedding_generation.generate_embeddings.torch")
        mock_torch.backends.mps.is_available.return_value = True
        mock_model = mocker.MagicMock()
        mock_model.get_sentence_embedding_dimension.return_value = 1024
        mock_st.return_value = mock_model

        from ingpipe_embedding_generation.generate_embeddings import _get_embedding_model

        result1 = _get_embedding_model("test-model")
        result2 = _get_embedding_model("test-model")

        assert result1 is mock_model
        assert result2 is mock_model
        # Called once (cached), and on the MPS device this host reports available.
        mock_st.assert_called_once_with("test-model", device="mps")

    def test_get_embedding_model_falls_back_to_cpu_without_mps(
        self, mocker, monkeypatch
    ) -> None:
        """Without MPS (the deployment VM), the model loads on the CPU device."""
        from ingpipe_embedding_generation import generate_embeddings

        monkeypatch.setattr(generate_embeddings, "_model", None)
        monkeypatch.setattr(generate_embeddings, "_model_name", None)

        mock_st = mocker.patch(
            "ingpipe_embedding_generation.generate_embeddings.SentenceTransformer"
        )
        mock_torch = mocker.patch("ingpipe_embedding_generation.generate_embeddings.torch")
        mock_torch.backends.mps.is_available.return_value = False
        mock_model = mocker.MagicMock()
        mock_model.get_sentence_embedding_dimension.return_value = 1024
        mock_st.return_value = mock_model

        from ingpipe_embedding_generation.generate_embeddings import _get_embedding_model

        result = _get_embedding_model("test-model")

        assert result is mock_model
        mock_st.assert_called_once_with("test-model", device="cpu")

    def test_get_embedding_model_reloads_on_name_change(
        self, mocker, monkeypatch
    ) -> None:
        """Model is reloaded when a different model name is requested."""
        from ingpipe_embedding_generation import generate_embeddings

        # Reset the module-level cache with teardown (see the test above).
        monkeypatch.setattr(generate_embeddings, "_model", None)
        monkeypatch.setattr(generate_embeddings, "_model_name", None)

        mock_st = mocker.patch(
            "ingpipe_embedding_generation.generate_embeddings.SentenceTransformer"
        )
        mocker.patch("ingpipe_embedding_generation.generate_embeddings.torch")
        mock_model_a = mocker.MagicMock()
        mock_model_a.get_sentence_embedding_dimension.return_value = 768
        mock_model_b = mocker.MagicMock()
        mock_model_b.get_sentence_embedding_dimension.return_value = 1024
        mock_st.side_effect = [mock_model_a, mock_model_b]

        from ingpipe_embedding_generation.generate_embeddings import _get_embedding_model

        result1 = _get_embedding_model("model-a")
        result2 = _get_embedding_model("model-b")

        assert result1 is mock_model_a
        assert result2 is mock_model_b
        assert mock_st.call_count == 2


def _select_connection() -> MockType:
    """Return the mock READ connection wired by ``_setup_generate_embeddings_mocks``.

    The SELECT for rows needing embeddings runs on the connection yielded by
    ``engine.connect()``. This accessor is the single place that encodes that
    mock shape, so a harness change lands in one spot.
    """
    return (
        generate_embeddings_module.get_engine.return_value.connect.return_value.__enter__.return_value
    )


def _write_connection() -> MockType:
    """Return the mock WRITE connection wired by ``_setup_generate_embeddings_mocks``.

    The overwrite DELETE and the batch INSERTs run on the connection yielded by
    ``engine.begin()`` (one transaction per batch).
    """
    return (
        generate_embeddings_module.get_engine.return_value.begin.return_value.__enter__.return_value
    )


def _captured_stream_select() -> tuple[str, dict[str, object]]:
    """Return (sql_text, bound_params) for the streamed row SELECT.

    The read connection now carries two statements — the up-front row count and
    the streamed row SELECT — so the row SELECT is the one that is not the count.

    Raises:
        AssertionError: If no streamed row SELECT was executed.
    """
    for call in _select_connection().execute.call_args_list:
        sql_text = str(call.args[0].text)
        if "count(*)" not in sql_text:
            return sql_text, call.args[1]
    raise AssertionError("no streamed row SELECT executed on the read connection")


def _captured_count_sql() -> str:
    """Return the up-front row-count query's text from the read connection.

    Raises:
        AssertionError: If no count query was executed.
    """
    for call in _select_connection().execute.call_args_list:
        sql_text = str(call.args[0].text)
        if "count(*)" in sql_text:
            return sql_text
    raise AssertionError("no count query executed on the read connection")


def _insert_param_batches() -> list[list[dict[str, object]]]:
    """Return each INSERT's bound params, one entry per flush.

    The insert is an executemany, so every entry is that batch's list of
    per-chunk param dicts — the shape a test asserts on to pin one round trip
    per batch (rather than one per chunk).
    """
    return [
        call.args[1]
        for call in _write_connection().execute.call_args_list
        if "insert into" in str(call.args[0].text)
    ]


def _setup_generate_embeddings_mocks(
    mocker,
    chunk_return: list[tuple[dict[str, object], int]],
    insert_calls: list[dict[str, object]],
    token_count: int = 4,
    source_rows: list[tuple] | None = None,
    row_stream: Iterator[tuple] | None = None,
) -> tuple[MockType, MockType]:
    """Wire up DB/model/chunker mocks for a generate_embeddings invocation.

    Stubs the engine, pre-flight helpers, model, and tokenizer so no real model
    or database is touched. The read connection serves both statements the
    production loop issues: the up-front ``count(*)`` over the filtered
    anti-join (answered with ``len(source_rows)``) and the streamed row SELECT,
    which is answered with an ITERATOR because production iterates the result
    instead of calling ``fetchall()``. Each batch INSERT is an executemany, so
    its params are a list of per-chunk dicts; those are flattened into
    ``insert_calls`` (one entry per chunk) and are filtered to INSERT statements,
    so an overwrite run's DELETE params never land there. ``model.encode`` is
    captured on the returned mock model so a test can assert what text reached
    the vector and the stored row.

    Args:
        mocker: pytest-mock fixture.
        chunk_return: Value the patched ``chunk_long_sections`` returns
            (list of ``(chunk_dict, chunk_number)`` tuples). A multi-row test
            that needs distinct chunks per row sets ``side_effect`` on the
            returned mock instead.
        insert_calls: List that every inserted chunk's bound params dict is
            appended to (each executemany batch is flattened into it).
        token_count: Constant token count the mocked tokenizer reports for any
            string, so the reserved header budget is deterministic.
        source_rows: Rows the patched SELECT streams, as tuples in
            ``column_names`` order (see the column-order note below), and whose
            length the count query reports. Defaults to a single row when None.
        row_stream: Optional iterator the streamed SELECT returns instead of
            ``iter(source_rows)`` — used by the streaming test to observe when
            rows are pulled relative to flushes. The count still reports
            ``len(source_rows)``.

    Returns:
        Tuple of (mock_model, mock_chunk) — the mocked model (with ``encode``
        capturing its input) and the patched ``chunk_long_sections`` mock.
    """
    import numpy as np

    mocker.patch(
        "ingpipe_embedding_generation.generate_embeddings.get_engine",
        return_value=mocker.MagicMock(),
    )
    # The extension preflight queries pg_extension on a real connection; the
    # mocked engine cannot answer it, and the contract itself is covered by
    # ingpipe-lib's own tests plus the real-database suite.
    mocker.patch("ingpipe_embedding_generation.generate_embeddings.require_extensions")
    mocker.patch("ingpipe_embedding_generation.generate_embeddings._verify_source_table_exists")
    mocker.patch(
        "ingpipe_embedding_generation.generate_embeddings._detect_primary_keys",
        return_value=["filename", "sort_order"],
    )
    mocker.patch("ingpipe_embedding_generation.generate_embeddings._validate_config_identifiers")
    mocker.patch("ingpipe_embedding_generation.generate_embeddings._create_embedding_table")

    # One source row needing an embedding. Column order follows
    # column_names = pk_columns + value_columns, where value_columns is
    # embed_columns then any header_columns not already in embed_columns. The
    # header tests use embed_columns=["content_text"], header_columns=["heading_text"]
    # => (filename, sort_order, content_text/body, heading_text). The empty-header
    # test passes embed_columns=["heading_text", "content_text"] and reads only
    # the first two value slots via its own chunk_return.
    select_conn = mocker.MagicMock()
    if source_rows is None:
        source_rows = [("ge101.pdf", 1, "Body text", "Chapter 1 Heading")]
    rows_for_count = source_rows

    def _dispatch_read(sql, params=None):
        # The count answers the row total; anything else is the streamed SELECT.
        if "count(*)" in str(sql):
            count_result = mocker.MagicMock()
            count_result.scalar_one.return_value = len(rows_for_count)
            return count_result
        return iter(source_rows) if row_stream is None else row_stream

    select_conn.execute.side_effect = _dispatch_read
    # The real Connection.execution_options() applies the streaming options in
    # place and returns the SAME Connection, so the streamed execute is captured
    # on this mock alongside the count.
    select_conn.execution_options.return_value = select_conn

    # Separate connection for the INSERT batch so we can capture bound params.
    insert_conn = mocker.MagicMock()

    def _capture_insert(_sql, params=None):
        # Only INSERTs: the same write connection also carries the overwrite
        # DELETE, whose filter params would otherwise be mistaken for a chunk.
        # An INSERT's params is the batch's list of dicts (executemany), so it is
        # flattened here to keep one entry per inserted chunk.
        if params is not None and "insert into" in str(_sql):
            insert_calls.extend(params)
        return mocker.MagicMock()

    insert_conn.execute.side_effect = _capture_insert

    engine = generate_embeddings_module.get_engine.return_value
    engine.connect.return_value.__enter__ = mocker.MagicMock(return_value=select_conn)
    engine.connect.return_value.__exit__ = mocker.MagicMock(return_value=False)
    engine.begin.return_value.__enter__ = mocker.MagicMock(return_value=insert_conn)
    engine.begin.return_value.__exit__ = mocker.MagicMock(return_value=False)

    mock_model = mocker.MagicMock()
    mock_model.get_sentence_embedding_dimension.return_value = 1024
    mock_model.tokenizer = mocker.MagicMock(
        return_value={"input_ids": list(range(token_count))}
    )
    # Sized from the call's own text list: batches vary in chunk count now that
    # rows are packed as they stream, so a fixed-shape return would misalign.
    mock_model.encode.side_effect = lambda texts, **kwargs: np.zeros((len(texts), 1024))
    mocker.patch(
        "ingpipe_embedding_generation.generate_embeddings._get_embedding_model",
        return_value=mock_model,
    )

    mock_chunk = mocker.patch(
        "ingpipe_embedding_generation.generate_embeddings.chunk_long_sections",
        return_value=chunk_return,
    )
    return mock_model, mock_chunk


class TestTokenCounter:
    """Tests for the shared token-counter wiring from _utils."""

    def test_make_token_counter_uses_model_tokenizer_input_ids(self, mocker) -> None:
        """count_tokens reports len(input_ids) from the model's tokenizer, no truncation."""
        mock_tokenizer = mocker.MagicMock(return_value={"input_ids": [1, 2, 3, 4, 5]})
        mock_model = mocker.MagicMock()
        mock_model.tokenizer = mock_tokenizer

        count_tokens = make_token_counter(mock_model)
        n = count_tokens("some text")

        assert n == 5
        mock_tokenizer.assert_called_once_with("some text", truncation=False)

    def test_get_tokenizer_falls_back_to_first_module(self, mocker) -> None:
        """When model has no .tokenizer, get_tokenizer uses _first_module().tokenizer."""
        mock_model = mocker.MagicMock()
        # Remove the auto-created .tokenizer so getattr(model, "tokenizer", None) is None.
        del mock_model.tokenizer
        sentinel_tokenizer = mocker.sentinel.tokenizer
        mock_model._first_module.return_value.tokenizer = sentinel_tokenizer

        result = get_tokenizer(mock_model)

        assert result is sentinel_tokenizer
        mock_model._first_module.assert_called_once_with()


class TestChunkingWiring:
    """Tests that generate_embeddings drives the chunker with the token budget."""

    def test_chunk_long_sections_called_with_token_budget_and_counter(self, mocker) -> None:
        """chunk_long_sections receives max_tokens, overlap_tokens, and a token counter.

        The model and its tokenizer are fully mocked (via the shared harness, so
        the streamed-read mock shape lives in one place), and the token counter is
        the real callable built over the mocked tokenizer — no real model loads.
        """
        from ingpipe_embedding_generation import generate_embeddings

        # One source row needing an embedding; a single passthrough chunk back.
        insert_calls: list = []
        _, mock_chunk = _setup_generate_embeddings_mocks(
            mocker,
            [({"content_text": "Body text", "word_count": 2}, 1)],
            insert_calls,
            token_count=3,
            source_rows=[("ge101.pdf", 1, "Heading", "Body text")],
        )

        generate_embeddings.generate_embeddings(
            db_name="policy_db",
            db_schema="cms_iom",
            source_table="document_content",
            embedding_table="document_content_embedding",
            embed_columns=["heading_text", "content_text"],
            model_name="Alibaba-NLP/gte-large-en-v1.5",
            batch_size=64,
            max_tokens=500,
            overlap_tokens=50,
            overwrite=False,
        )

        mock_chunk.assert_called_once()
        call_args = mock_chunk.call_args[0]
        # Positional: (sections, max_tokens, overlap_tokens, count_tokens)
        assert call_args[1] == 500
        assert call_args[2] == 50
        assert callable(call_args[3])


class TestContextualChunkHeaders:
    """Tests for the header_columns contextual-chunk-header feature."""

    def test_generate_embeddings_header_prepended_to_every_chunk_including_tail(self, mocker) -> None:
        """The header prefix is prepended to EVERY chunk, including chunk_number >= 2.

        Asserts on the assembled per-chunk text for both the stored chunk_text
        and the embedding input, and that word_count stays the body chunk's
        count (not header+body). Model and tokenizer are fully mocked.
        """
        # Two body chunks so we can assert the tail chunk (chunk_number=2) also
        # carries the header. word_count values are the body chunk's counts.
        chunk_return = [
            ({"content_text": "first chunk body", "word_count": 3}, 1),
            ({"content_text": "second chunk body", "word_count": 3}, 2),
        ]
        insert_calls: list = []
        mock_model, _mock_chunk = _setup_generate_embeddings_mocks(
            mocker, chunk_return, insert_calls, token_count=4
        )

        generate_embeddings_module.generate_embeddings(
            db_name="policy_db",
            db_schema="cms_iom",
            source_table="document_content",
            embedding_table="document_content_embedding",
            embed_columns=["content_text"],
            model_name="Alibaba-NLP/gte-large-en-v1.5",
            batch_size=64,
            max_tokens=500,
            overlap_tokens=50,
            overwrite=False,
            header_columns=["heading_text"],
        )

        header_prefix = "heading_text: Chapter 1 Heading\n"

        # Both stored chunks (chunk_number 1 AND 2) begin with the header.
        assert len(insert_calls) == 2
        stored_by_chunk = {c["chunk_number"]: c for c in insert_calls}
        assert stored_by_chunk[1]["chunk_text"] == header_prefix + "first chunk body"
        assert stored_by_chunk[2]["chunk_text"] == header_prefix + "second chunk body"

        # word_count is the body chunk's count, NOT recounted over header+body.
        assert stored_by_chunk[1]["word_count"] == 3
        assert stored_by_chunk[2]["word_count"] == 3

        # The embedding input (vector) carries the header too — must match storage.
        encoded_texts = mock_model.encode.call_args[0][0]
        assert encoded_texts == [
            header_prefix + "first chunk body",
            header_prefix + "second chunk body",
        ]

    def test_generate_embeddings_body_chunked_with_header_reserved_budget(self, mocker) -> None:
        """chunk_long_sections receives max_tokens minus the header's token cost.

        The mocked tokenizer reports a constant token count, so the reserved
        header cost is deterministic and the effective body budget is
        max_tokens - header_cost.
        """
        chunk_return = [({"content_text": "body", "word_count": 1}, 1)]
        insert_calls: list = []
        # Constant tokenizer count of 7 => header_cost is 7.
        _setup_generate_embeddings_mocks(
            mocker, chunk_return, insert_calls, token_count=7
        )
        mock_chunk = generate_embeddings_module.chunk_long_sections

        generate_embeddings_module.generate_embeddings(
            db_name="policy_db",
            db_schema="cms_iom",
            source_table="document_content",
            embedding_table="document_content_embedding",
            embed_columns=["content_text"],
            model_name="Alibaba-NLP/gte-large-en-v1.5",
            batch_size=64,
            max_tokens=500,
            overlap_tokens=50,
            overwrite=False,
            header_columns=["heading_text"],
        )

        mock_chunk.assert_called_once()
        call_args = mock_chunk.call_args[0]
        # Positional: (sections, effective_budget, overlap_tokens, count_tokens)
        assert call_args[0] == [{"content_text": "content_text: Body text"}]
        assert call_args[1] == 500 - 7
        assert call_args[2] == 50

    def test_generate_embeddings_empty_header_columns_unchanged_behavior(self, mocker) -> None:
        """With header_columns=[] the chunker gets the FULL max_tokens budget.

        Backward-compatibility canary: no header is prepended and word_count is
        unchanged, matching today's whole-row chunking behavior.
        """
        chunk_return = [
            ({"content_text": "heading_text: H\ncontent_text: Body", "word_count": 6}, 1)
        ]
        insert_calls: list = []
        _setup_generate_embeddings_mocks(
            mocker, chunk_return, insert_calls, token_count=4
        )
        mock_chunk = generate_embeddings_module.chunk_long_sections

        generate_embeddings_module.generate_embeddings(
            db_name="policy_db",
            db_schema="cms_iom",
            source_table="document_content",
            embedding_table="document_content_embedding",
            embed_columns=["heading_text", "content_text"],
            model_name="Alibaba-NLP/gte-large-en-v1.5",
            batch_size=64,
            max_tokens=500,
            overlap_tokens=50,
            overwrite=False,
            header_columns=[],
        )

        mock_chunk.assert_called_once()
        call_args = mock_chunk.call_args[0]
        # Full budget, no header reservation.
        assert call_args[1] == 500
        assert call_args[2] == 50
        # Stored text is exactly the chunker output, no prepended header.
        assert insert_calls[0]["chunk_text"] == "heading_text: H\ncontent_text: Body"

    def test_generate_embeddings_header_budget_floored_above_overlap_for_oversize_header(self, mocker) -> None:
        """A header costing more than max_tokens - overlap floors the body budget.

        Guards the chunker's overlap_tokens < max_tokens invariant: the
        effective budget is floored to overlap_tokens + 1 rather than going
        non-positive.
        """
        chunk_return = [({"content_text": "body", "word_count": 1}, 1)]
        insert_calls: list = []
        # header_cost == max_tokens leaves effective budget 0 <= overlap_tokens.
        _setup_generate_embeddings_mocks(
            mocker, chunk_return, insert_calls, token_count=100
        )
        mock_chunk = generate_embeddings_module.chunk_long_sections

        generate_embeddings_module.generate_embeddings(
            db_name="policy_db",
            db_schema="cms_iom",
            source_table="document_content",
            embedding_table="document_content_embedding",
            embed_columns=["content_text"],
            model_name="Alibaba-NLP/gte-large-en-v1.5",
            batch_size=64,
            max_tokens=100,
            overlap_tokens=50,
            overwrite=False,
            header_columns=["heading_text"],
        )

        mock_chunk.assert_called_once()
        call_args = mock_chunk.call_args[0]
        # 100 - 100 = 0 <= overlap_tokens(50) => floored to 51.
        assert call_args[1] == 51
        assert call_args[2] == 50


class TestEmbeddingTableComment:
    """Tests for the resolved COMMENT ON text handed to _create_embedding_table."""

    _COMMON: ClassVar[dict] = dict(
        db_name="policy_db",
        db_schema="cms_iom",
        source_table="document_content",
        embedding_table="document_content_embedding",
        model_name="Alibaba-NLP/gte-large-en-v1.5",
        batch_size=64,
        max_tokens=500,
        overlap_tokens=50,
        overwrite=False,
    )

    @staticmethod
    def _resolved_comment() -> str:
        """Return the table_comment argument passed to the mocked creator.

        Bound by name against the real signature (the module-level import still
        references the unpatched function), so inserting a parameter ahead of
        table_comment cannot silently shift this assertion onto another arg.
        """
        import inspect as py_inspect

        call_args = generate_embeddings_module._create_embedding_table.call_args
        bound = py_inspect.signature(_create_embedding_table).bind(
            *call_args.args, **call_args.kwargs
        )
        return bound.arguments["table_comment"]

    def test_generate_embeddings_default_comment_with_header_columns(self, mocker) -> None:
        """With header_columns set, the default text claims the header prefix."""
        chunk_return = [({"content_text": "body", "word_count": 1}, 1)]
        _setup_generate_embeddings_mocks(mocker, chunk_return, [], token_count=4)

        generate_embeddings_module.generate_embeddings(
            embed_columns=["content_text"],
            header_columns=["heading_text"],
            **self._COMMON,
        )

        assert self._resolved_comment() == (
            "Header-prefixed chunks of cms_iom.document_content for hybrid "
            "retrieval (embedding HNSW + chunk_tsv GIN); backs the search tool"
        )

    def test_generate_embeddings_default_comment_without_header_columns(self, mocker) -> None:
        """Without header_columns, the default text drops the header claim."""
        chunk_return = [({"content_text": "body", "word_count": 1}, 1)]
        _setup_generate_embeddings_mocks(mocker, chunk_return, [], token_count=4)

        generate_embeddings_module.generate_embeddings(
            embed_columns=["heading_text", "content_text"],
            **self._COMMON,
        )

        assert self._resolved_comment() == (
            "Chunks of cms_iom.document_content for hybrid retrieval "
            "(embedding HNSW + chunk_tsv GIN); backs the search tool"
        )

    def test_generate_embeddings_override_comment_replaces_default(self, mocker) -> None:
        """An explicit table_comment reaches the creator unchanged."""
        chunk_return = [({"content_text": "body", "word_count": 1}, 1)]
        _setup_generate_embeddings_mocks(mocker, chunk_return, [], token_count=4)

        generate_embeddings_module.generate_embeddings(
            embed_columns=["content_text"],
            header_columns=["heading_text"],
            table_comment="Custom flavored description",
            **self._COMMON,
        )

        assert self._resolved_comment() == "Custom flavored description"

    def test_generate_embeddings_non_string_table_comment_raises(self, mocker) -> None:
        """A non-string table_comment is a config error (ConfigurationError)."""
        chunk_return = [({"content_text": "body", "word_count": 1}, 1)]
        _setup_generate_embeddings_mocks(mocker, chunk_return, [], token_count=4)

        with pytest.raises(ConfigurationError, match="table_comment must be a string"):
            generate_embeddings_module.generate_embeddings(
                embed_columns=["content_text"],
                table_comment=42,
                **self._COMMON,
            )


class TestGenerateEmbeddingsResilience:
    """Tests for generate_embeddings resilience branches (filter validation, overwrite, no-rows)."""

    def test_generate_embeddings_source_filter_unknown_column_raises(self, mocker) -> None:
        """A source_filter key absent from the source table raises ConfigurationError."""
        mocker.patch(
            "ingpipe_embedding_generation.generate_embeddings.get_engine",
            return_value=mocker.MagicMock(),
        )
        mocker.patch("ingpipe_embedding_generation.generate_embeddings.require_extensions")
        mocker.patch("ingpipe_embedding_generation.generate_embeddings._verify_source_table_exists")
        mocker.patch(
            "ingpipe_embedding_generation.generate_embeddings._detect_primary_keys",
            return_value=["filename", "sort_order"],
        )
        mocker.patch(
            "ingpipe_embedding_generation.generate_embeddings._validate_config_identifiers"
        )
        mocker.patch("ingpipe_embedding_generation.generate_embeddings._create_embedding_table")
        mocker.patch(
            "ingpipe_embedding_generation.generate_embeddings._get_embedding_model",
            return_value=mocker.MagicMock(),
        )

        mock_inspect = mocker.patch("ingpipe_embedding_generation.generate_embeddings.inspect")
        mock_insp = mocker.MagicMock()
        mock_insp.get_columns.return_value = [
            {"name": "filename"},
            {"name": "sort_order"},
            {"name": "content_text"},
        ]
        mock_inspect.return_value = mock_insp

        with pytest.raises(ConfigurationError, match="columns not on"):
            generate_embeddings_module.generate_embeddings(
                db_name="policy_db",
                db_schema="cms_iom",
                source_table="document_content",
                embedding_table="document_content_embedding",
                embed_columns=["content_text"],
                model_name="Alibaba-NLP/gte-large-en-v1.5",
                batch_size=64,
                max_tokens=500,
                overlap_tokens=50,
                overwrite=False,
                source_filter={"not_a_col": "x%"},
            )

    def test_generate_embeddings_no_rows_returns_zero(self, mocker) -> None:
        """When no source rows need embeddings, returns 0 and inserts nothing.

        The early return is driven by the up-front count reporting 0, so the
        streamed SELECT is never even issued.
        """
        chunk_return = [({"content_text": "body", "word_count": 1}, 1)]
        insert_calls: list = []
        # No source rows => the count query reports 0.
        _setup_generate_embeddings_mocks(
            mocker, chunk_return, insert_calls, token_count=4, source_rows=[]
        )

        result = generate_embeddings_module.generate_embeddings(
            db_name="policy_db",
            db_schema="cms_iom",
            source_table="document_content",
            embedding_table="document_content_embedding",
            embed_columns=["heading_text", "content_text"],
            model_name="Alibaba-NLP/gte-large-en-v1.5",
            batch_size=64,
            max_tokens=500,
            overlap_tokens=50,
            overwrite=False,
        )

        assert result == 0
        _write_connection().execute.assert_not_called()
        # Only the count ran on the read connection — no streamed SELECT.
        assert len(_select_connection().execute.call_args_list) == 1
        assert "count(*)" in _captured_count_sql()

    def test_generate_embeddings_zero_chunk_row_skipped_and_counted(
        self, mocker, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A row whose text yields no chunks is skipped with visible warnings.

        The chunker returns nothing for the first row (whitespace-only text)
        but a real chunk for the second: the first row is skipped (no insert),
        named in a WARNING, and counted in the skip summary, while the second
        row still inserts.
        """
        insert_calls: list = []
        _, mock_chunk = _setup_generate_embeddings_mocks(
            mocker,
            chunk_return=[],
            insert_calls=insert_calls,
            token_count=4,
            source_rows=[
                ("ws.pdf", 1, "Heading A", "\n\n\n"),
                ("ok.pdf", 2, "Heading B", "Body B"),
            ],
        )
        mock_chunk.side_effect = [
            [],
            [({"content_text": "body", "word_count": 1}, 1)],
        ]

        with caplog.at_level("WARNING"):
            result = generate_embeddings_module.generate_embeddings(
                db_name="policy_db",
                db_schema="cms_iom",
                source_table="document_content",
                embedding_table="document_content_embedding",
                embed_columns=["heading_text", "content_text"],
                model_name="Alibaba-NLP/gte-large-en-v1.5",
                batch_size=64,
                max_tokens=500,
                overlap_tokens=50,
                overwrite=False,
            )

        # Only the second row inserted; the first is skipped, not silently
        # counted as processed.
        assert result == 1
        assert len(insert_calls) == 1
        assert insert_calls[0]["filename"] == "ok.pdf"
        messages = [record.message for record in caplog.records]
        assert any("produced no chunks" in m and "ws.pdf" in m for m in messages)
        assert any("Skipped 1/2 row(s)" in m for m in messages)

    def test_generate_embeddings_all_empty_row_skipped_heading_only_kept(
        self, mocker, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A row with every embedded value empty skips; heading-only embeds.

        The all-empty row is the case that previously stored an embedding of
        the empty string. A heading-only row (NULL body, heading present)
        carries real meaning and must still embed.
        """
        insert_calls: list = []
        _setup_generate_embeddings_mocks(
            mocker,
            chunk_return=[({"content_text": "body", "word_count": 1}, 1)],
            insert_calls=insert_calls,
            token_count=4,
            source_rows=[
                # (filename, sort_order, heading_text, content_text)
                ("empty.pdf", 1, None, "   "),
                ("headingonly.pdf", 2, "Chapter 1", None),
            ],
        )

        with caplog.at_level("WARNING"):
            result = generate_embeddings_module.generate_embeddings(
                db_name="policy_db",
                db_schema="cms_iom",
                source_table="document_content",
                embedding_table="document_content_embedding",
                embed_columns=["heading_text", "content_text"],
                model_name="stub",
                batch_size=64,
                max_tokens=500,
                overlap_tokens=50,
                overwrite=False,
            )

        assert result == 1
        assert len(insert_calls) == 1
        assert insert_calls[0]["filename"] == "headingonly.pdf"
        messages = [record.message for record in caplog.records]
        assert any(
            "no content in any embedded or header column" in m and "empty.pdf" in m
            for m in messages
        )

    def test_generate_embeddings_disposes_engine_in_finally(self, mocker) -> None:
        """The per-table engine is disposed even when the run raises.

        Without the dispose an N-table run leaks N pooled connection sets for
        the process lifetime.
        """
        chunk_return = [({"content_text": "body", "word_count": 1}, 1)]
        insert_calls: list = []
        _setup_generate_embeddings_mocks(mocker, chunk_return, insert_calls)
        engine = generate_embeddings_module.get_engine.return_value

        # Success path disposes once.
        generate_embeddings_module.generate_embeddings(
            db_name="policy_db",
            db_schema="cms_iom",
            source_table="document_content",
            embedding_table="document_content_embedding",
            embed_columns=["heading_text", "content_text"],
            model_name="stub",
            batch_size=64,
            max_tokens=500,
            overlap_tokens=50,
            overwrite=False,
        )
        assert engine.dispose.call_count == 1

        # Failure path (table creation raises) still disposes.
        mocker.patch(
            "ingpipe_embedding_generation.generate_embeddings._create_embedding_table",
            side_effect=ConfigurationError("boom"),
        )
        with pytest.raises(ConfigurationError):
            generate_embeddings_module.generate_embeddings(
                db_name="policy_db",
                db_schema="cms_iom",
                source_table="document_content",
                embedding_table="document_content_embedding",
                embed_columns=["heading_text", "content_text"],
                model_name="stub",
                batch_size=64,
                max_tokens=500,
                overlap_tokens=50,
                overwrite=False,
            )
        assert engine.dispose.call_count == 2

    def test_generate_embeddings_over_long_embedding_table_name_raises(
        self, mocker
    ) -> None:
        """A table name overflowing the 63-byte index-name limit fails loudly.

        `create index if not exists` matches on the truncated name, so an
        over-long derived index name could silently match a differently
        intended index and leave the table without its own.
        """
        # 50 chars + "idx_" + "_embedding_hnsw" = 69 bytes > 63. Calls the
        # REAL _create_embedding_table (no harness — it would patch it away);
        # the guard fires before any engine use, so a bare mock engine works.
        long_table = "x" * 50

        with pytest.raises(ConfigurationError, match="63-byte identifier limit"):
            _create_embedding_table(
                engine=mocker.MagicMock(),
                db_schema="cms_iom",
                source_table="document_content",
                embedding_table=long_table,
                pk_columns=["filename"],
                embedding_dimension=4,
                table_comment="text",
            )

    def test_generate_embeddings_rows_packed_into_batches_on_row_boundaries(self, mocker) -> None:
        """Multiple rows with a small batch_size flush as separate whole-row
        batches (the mid-loop batch boundary); every row's chunk is inserted."""
        chunk_return = [({"content_text": "body", "word_count": 1}, 1)]
        insert_calls: list = []
        _setup_generate_embeddings_mocks(
            mocker,
            chunk_return,
            insert_calls,
            token_count=4,
            source_rows=[
                ("ge101.pdf", 1, "Heading A", "Body A"),
                ("ge102.pdf", 2, "Heading B", "Body B"),
            ],
        )

        result = generate_embeddings_module.generate_embeddings(
            db_name="policy_db",
            db_schema="cms_iom",
            source_table="document_content",
            embedding_table="document_content_embedding",
            embed_columns=["heading_text", "content_text"],
            model_name="Alibaba-NLP/gte-large-en-v1.5",
            batch_size=1,
            max_tokens=500,
            overlap_tokens=50,
            overwrite=False,
        )

        # Two rows, batch_size=1 -> two separate flushes; both rows inserted.
        assert result == 2
        assert len(insert_calls) == 2
        assert {c["filename"] for c in insert_calls} == {"ge101.pdf", "ge102.pdf"}


class TestStreamedBatchInserts:
    """Tests for the streamed source read and the per-batch executemany insert.

    These pin the contracts the streaming refactor rests on: one INSERT round
    trip per batch, a row's chunks never split across flushes (whole-row
    atomicity), and flushing interleaved with the source stream rather than after
    it is drained.
    """

    _COMMON: ClassVar[dict] = dict(
        db_name="policy_db",
        db_schema="cms_iom",
        source_table="document_content",
        embedding_table="document_content_embedding",
        embed_columns=["heading_text", "content_text"],
        model_name="Alibaba-NLP/gte-large-en-v1.5",
        max_tokens=500,
        overlap_tokens=50,
        overwrite=False,
    )

    @staticmethod
    def _one_chunk_per_row(text_prefix: str, count: int) -> list[list[tuple[dict, int]]]:
        """Return per-row chunker outputs of one chunk each.

        Args:
            text_prefix: Prefix for each chunk's content, made unique per row.
            count: Number of rows (and therefore chunker calls) to produce.

        Returns:
            A list of ``chunk_long_sections`` return values, one per row.
        """
        return [
            [({"content_text": f"{text_prefix}{i}", "word_count": 1}, 1)]
            for i in range(count)
        ]

    def test_generate_embeddings_insert_is_one_executemany_per_batch(self, mocker) -> None:
        """Each flush issues ONE INSERT whose params is a list of the batch's chunks.

        Three single-chunk rows with batch_size=2 pack as [row1+row2], [row3], so
        two INSERT statements carry 2 and 1 param dicts — not one statement per
        chunk.
        """
        insert_calls: list = []
        _, mock_chunk = _setup_generate_embeddings_mocks(
            mocker,
            [({"content_text": "body", "word_count": 1}, 1)],
            insert_calls,
            token_count=4,
            source_rows=[
                ("ge101.pdf", 1, "Heading A", "Body A"),
                ("ge102.pdf", 2, "Heading B", "Body B"),
                ("ge103.pdf", 3, "Heading C", "Body C"),
            ],
        )
        mock_chunk.side_effect = self._one_chunk_per_row("body ", 3)

        result = generate_embeddings_module.generate_embeddings(
            batch_size=2, **self._COMMON
        )

        assert result == 3
        batches = _insert_param_batches()
        assert len(batches) == 2
        for params in batches:
            assert isinstance(params, list)
            assert all(isinstance(p, dict) for p in params)
        assert [len(params) for params in batches] == [2, 1]
        # Every chunk still reached the database exactly once.
        assert [c["filename"] for c in insert_calls] == [
            "ge101.pdf",
            "ge102.pdf",
            "ge103.pdf",
        ]

    def test_generate_embeddings_multi_chunk_row_stays_in_one_flush(self, mocker) -> None:
        """A row's chunks are never split across flushes (whole-row atomicity).

        Two rows of two chunks each with batch_size=3 cannot share a batch, so
        each flush carries exactly one row's complete pair of chunks.
        """
        insert_calls: list = []
        _, mock_chunk = _setup_generate_embeddings_mocks(
            mocker,
            [({"content_text": "unused", "word_count": 1}, 1)],
            insert_calls,
            token_count=4,
            source_rows=[
                ("ge101.pdf", 1, "Heading A", "Body A"),
                ("ge102.pdf", 2, "Heading B", "Body B"),
            ],
        )
        mock_chunk.side_effect = [
            [
                ({"content_text": "A1", "word_count": 1}, 1),
                ({"content_text": "A2", "word_count": 1}, 2),
            ],
            [
                ({"content_text": "B1", "word_count": 1}, 1),
                ({"content_text": "B2", "word_count": 1}, 2),
            ],
        ]

        result = generate_embeddings_module.generate_embeddings(
            batch_size=3, **self._COMMON
        )

        assert result == 4
        batches = _insert_param_batches()
        assert len(batches) == 2
        for params in batches:
            # One row per flush, and BOTH of its chunks in that same flush.
            assert len({p["filename"] for p in params}) == 1
            assert [p["chunk_number"] for p in params] == [1, 2]
        assert [params[0]["filename"] for params in batches] == [
            "ge101.pdf",
            "ge102.pdf",
        ]

    def test_generate_embeddings_row_exceeding_batch_size_flushes_alone(self, mocker) -> None:
        """A row with more chunks than batch_size becomes its own larger batch.

        Row one yields three chunks against batch_size=2, so it flushes alone
        (oversized but atomic) and row two flushes separately.
        """
        insert_calls: list = []
        _, mock_chunk = _setup_generate_embeddings_mocks(
            mocker,
            [({"content_text": "unused", "word_count": 1}, 1)],
            insert_calls,
            token_count=4,
            source_rows=[
                ("ge101.pdf", 1, "Heading A", "Body A"),
                ("ge102.pdf", 2, "Heading B", "Body B"),
            ],
        )
        mock_chunk.side_effect = [
            [
                ({"content_text": "A1", "word_count": 1}, 1),
                ({"content_text": "A2", "word_count": 1}, 2),
                ({"content_text": "A3", "word_count": 1}, 3),
            ],
            [({"content_text": "B1", "word_count": 1}, 1)],
        ]

        result = generate_embeddings_module.generate_embeddings(
            batch_size=2, **self._COMMON
        )

        assert result == 4
        batches = _insert_param_batches()
        assert len(batches) == 2
        # The oversized row's three chunks land in a single flush of its own.
        assert len(batches[0]) == 3
        assert {p["filename"] for p in batches[0]} == {"ge101.pdf"}
        assert [p["chunk_number"] for p in batches[0]] == [1, 2, 3]
        assert len(batches[1]) == 1
        assert batches[1][0]["filename"] == "ge102.pdf"

    def test_generate_embeddings_flushes_before_source_stream_is_exhausted(
        self, mocker
    ) -> None:
        """Inserts interleave with the source stream (streaming, not materializing).

        The source rows come from a generator that records how many chunks had
        already been inserted each time a row was pulled. With batch_size=1 and
        one chunk per row, a flush must have happened before the last row is
        pulled — under the old materialize-then-flush loop every recorded count
        would be zero.
        """
        insert_calls: list = []
        source_rows = [
            ("ge101.pdf", 1, "Heading A", "Body A"),
            ("ge102.pdf", 2, "Heading B", "Body B"),
            ("ge103.pdf", 3, "Heading C", "Body C"),
        ]
        inserted_at_pull: list[int] = []

        def _recording_stream() -> Iterator[tuple]:
            for row in source_rows:
                inserted_at_pull.append(len(insert_calls))
                yield row

        _, mock_chunk = _setup_generate_embeddings_mocks(
            mocker,
            [({"content_text": "body", "word_count": 1}, 1)],
            insert_calls,
            token_count=4,
            source_rows=source_rows,
            row_stream=_recording_stream(),
        )
        mock_chunk.side_effect = self._one_chunk_per_row("body ", 3)

        result = generate_embeddings_module.generate_embeddings(
            batch_size=1, **self._COMMON
        )

        assert result == 3
        # Every row was pulled, and inserts had already started before the last.
        assert len(inserted_at_pull) == 3
        assert inserted_at_pull[-1] > 0
        assert len(_insert_param_batches()) == 3

    def test_generate_embeddings_stream_read_requests_yield_per_batch_size(
        self, mocker
    ) -> None:
        """The row SELECT runs on a connection configured with yield_per=batch_size.

        yield_per implies stream_results (a psycopg2 server-side cursor), which is
        what bounds peak memory to a batch's worth of rows.
        """
        insert_calls: list = []
        _setup_generate_embeddings_mocks(
            mocker,
            [({"content_text": "body", "word_count": 1}, 1)],
            insert_calls,
            token_count=4,
        )

        generate_embeddings_module.generate_embeddings(batch_size=32, **self._COMMON)

        _select_connection().execution_options.assert_called_once_with(yield_per=32)


def _write_config_text(tmp_path: Path, body: str) -> str:
    """Write the given TOML body to ``tmp_path/config.toml`` and return its path.

    Args:
        tmp_path: pytest tmp_path fixture directory.
        body: Raw TOML text to write.

    Returns:
        Absolute path string to the written config file.
    """
    config_path = tmp_path / "config.toml"
    # tomllib requires UTF-8; write_text would otherwise use the platform
    # locale encoding (cp1252 on Windows) and mangle non-ASCII config text.
    config_path.write_text(body, encoding="utf-8")
    return str(config_path)


def _empty_env(tmp_path: Path) -> str:
    """Write an empty dotenv file for the required ``--env-file`` flag.

    These tests exercise config handling and pipeline wiring, not credentials:
    an empty file satisfies the flag while loading nothing, so the ambient
    test environment is untouched.

    Args:
        tmp_path: pytest tmp_path fixture directory.

    Returns:
        Absolute path string to the written (empty) dotenv file.
    """
    env_path = tmp_path / ".env.empty"
    env_path.write_text("", encoding="utf-8")
    return str(env_path)


class TestMain:
    """Tests for the main() entry point."""

    _BASE_HEADER = (
        'db_name = "policy_db"\n'
        'db_schema = "cms_iom"\n'
        'model_name = "Alibaba-NLP/gte-large-en-v1.5"\n'
    )

    def test_main_one_table_fails_others_still_processed_then_exits(
        self, mocker, tmp_path
    ) -> None:
        """A table raising ConfigurationError is recorded; the rest still run, then exit(1)."""
        config_path = _write_config_text(
            tmp_path,
            self._BASE_HEADER
            + '[tables.bad_table]\nembed_columns = ["content_text"]\n'
            + '[tables.good_table]\nembed_columns = ["content_text"]\n',
        )
        mocker.patch("ingpipe_embedding_generation.generate_embeddings.setup_entry_logging")
        mock_gen = mocker.patch(
            "ingpipe_embedding_generation.generate_embeddings.generate_embeddings",
            side_effect=[ConfigurationError("no primary key"), 5],
        )
        mocker.patch(
            "sys.argv",
            ["generate_embeddings.py", "--config", config_path, "--env-file", _empty_env(tmp_path)],
        )

        with pytest.raises(SystemExit) as excinfo:
            generate_embeddings_module.main()

        assert excinfo.value.code == 1
        # The good table was still processed after the bad one failed.
        assert mock_gen.call_count == 2

    def test_main_table_missing_embed_columns_is_skipped(self, mocker, tmp_path) -> None:
        """A table without embed_columns is skipped (never passed to generate_embeddings)."""
        config_path = _write_config_text(
            tmp_path,
            self._BASE_HEADER
            + '[tables.no_cols_table]\nembedding_table = "x_embedding"\n'
            + '[tables.good_table]\nembed_columns = ["content_text"]\n',
        )
        mocker.patch("ingpipe_embedding_generation.generate_embeddings.setup_entry_logging")
        mock_gen = mocker.patch(
            "ingpipe_embedding_generation.generate_embeddings.generate_embeddings", return_value=3
        )
        mocker.patch(
            "sys.argv",
            ["generate_embeddings.py", "--config", config_path, "--env-file", _empty_env(tmp_path)],
        )

        # One table failed (the skipped one) so main still exits non-zero.
        with pytest.raises(SystemExit) as excinfo:
            generate_embeddings_module.main()

        assert excinfo.value.code == 1
        # Only the valid table reached generate_embeddings.
        assert mock_gen.call_count == 1
        assert mock_gen.call_args.kwargs["source_table"] == "good_table"

    def test_main_cli_overwrite_flag_overrides_toml_default(
        self, mocker, tmp_path
    ) -> None:
        """The --overwrite CLI flag overrides the TOML overwrite default."""
        config_path = _write_config_text(
            tmp_path,
            self._BASE_HEADER
            + "overwrite = false\n"
            + '[tables.good_table]\nembed_columns = ["content_text"]\n',
        )
        mocker.patch("ingpipe_embedding_generation.generate_embeddings.setup_entry_logging")
        mock_gen = mocker.patch(
            "ingpipe_embedding_generation.generate_embeddings.generate_embeddings", return_value=1
        )
        mocker.patch(
            "sys.argv",
            [
                "generate_embeddings.py",
                "--config",
                config_path,
                "--overwrite",
                "--env-file",
                _empty_env(tmp_path),
            ],
        )

        generate_embeddings_module.main()

        assert mock_gen.call_args.kwargs["overwrite"] is True

    def test_main_non_boolean_overwrite_exits_before_any_table(
        self, mocker, tmp_path
    ) -> None:
        """A quoted top-level `overwrite = "false"` exits 1 before any deletion.

        Without the guard the truthy string would enable the destructive
        overwrite DELETE of existing embeddings. generate_embeddings is never
        reached, so no DB work (and no deletion) can occur.
        """
        config_path = _write_config_text(
            tmp_path,
            self._BASE_HEADER
            + 'overwrite = "false"\n'
            + '[tables.good_table]\nembed_columns = ["content_text"]\n',
        )
        mocker.patch("ingpipe_embedding_generation.generate_embeddings.setup_entry_logging")
        mock_gen = mocker.patch(
            "ingpipe_embedding_generation.generate_embeddings.generate_embeddings"
        )
        mocker.patch(
            "sys.argv",
            ["generate_embeddings.py", "--config", config_path, "--env-file", _empty_env(tmp_path)],
        )

        with pytest.raises(SystemExit) as excinfo:
            generate_embeddings_module.main()

        assert excinfo.value.code == 1
        mock_gen.assert_not_called()

    def test_main_non_boolean_per_table_overwrite_fails_that_table_only(
        self, mocker, tmp_path
    ) -> None:
        """A quoted per-table overwrite fails that table without reaching it.

        The offending table is recorded as failed (exit 1) and never passed to
        generate_embeddings; the sibling table with a real boolean still runs.
        """
        config_path = _write_config_text(
            tmp_path,
            self._BASE_HEADER
            + '[tables.bad_table]\nembed_columns = ["content_text"]\noverwrite = "true"\n'
            + '[tables.good_table]\nembed_columns = ["content_text"]\noverwrite = true\n',
        )
        mocker.patch("ingpipe_embedding_generation.generate_embeddings.setup_entry_logging")
        mock_gen = mocker.patch(
            "ingpipe_embedding_generation.generate_embeddings.generate_embeddings",
            return_value=2,
        )
        mocker.patch(
            "sys.argv",
            ["generate_embeddings.py", "--config", config_path, "--env-file", _empty_env(tmp_path)],
        )

        with pytest.raises(SystemExit) as excinfo:
            generate_embeddings_module.main()

        assert excinfo.value.code == 1
        # Only the boolean-valued table reached generate_embeddings, and its
        # real boolean passed through intact.
        assert mock_gen.call_count == 1
        assert mock_gen.call_args.kwargs["source_table"] == "good_table"
        assert mock_gen.call_args.kwargs["overwrite"] is True

    def test_main_table_comment_passes_through(self, mocker, tmp_path) -> None:
        """A per-table table_comment in the TOML reaches generate_embeddings."""
        config_path = _write_config_text(
            tmp_path,
            self._BASE_HEADER
            + "[tables.good_table]\n"
            + 'embed_columns = ["content_text"]\n'
            + 'table_comment = "Flavored embedding text"\n',
        )
        mocker.patch("ingpipe_embedding_generation.generate_embeddings.setup_entry_logging")
        mock_gen = mocker.patch(
            "ingpipe_embedding_generation.generate_embeddings.generate_embeddings", return_value=1
        )
        mocker.patch(
            "sys.argv",
            ["generate_embeddings.py", "--config", config_path, "--env-file", _empty_env(tmp_path)],
        )

        generate_embeddings_module.main()

        assert (
            mock_gen.call_args.kwargs["table_comment"] == "Flavored embedding text"
        )

    def test_main_missing_config_file_exits_one(self, mocker, tmp_path) -> None:
        """A non-existent config path exits with code 1."""
        missing = str(tmp_path / "does_not_exist.toml")
        mocker.patch("ingpipe_embedding_generation.generate_embeddings.setup_entry_logging")
        mocker.patch(
            "sys.argv",
            ["generate_embeddings.py", "--config", missing, "--env-file", _empty_env(tmp_path)],
        )

        with pytest.raises(SystemExit) as excinfo:
            generate_embeddings_module.main()

        assert excinfo.value.code == 1

    def test_main_missing_required_field_exits_one(self, mocker, tmp_path) -> None:
        """A config missing a required top-level field exits with code 1."""
        # Omit db_name (a required field).
        config_path = _write_config_text(
            tmp_path,
            'db_schema = "cms_iom"\n'
            'model_name = "Alibaba-NLP/gte-large-en-v1.5"\n'
            '[tables.good_table]\nembed_columns = ["content_text"]\n',
        )
        mocker.patch("ingpipe_embedding_generation.generate_embeddings.setup_entry_logging")
        mocker.patch(
            "sys.argv",
            ["generate_embeddings.py", "--config", config_path, "--env-file", _empty_env(tmp_path)],
        )

        with pytest.raises(SystemExit) as excinfo:
            generate_embeddings_module.main()

        assert excinfo.value.code == 1

    def test_main_invalid_toml_exits_one(self, mocker, tmp_path) -> None:
        """A config file that is not parseable TOML exits with code 1."""
        config_path = _write_config_text(tmp_path, "not = valid = toml\n")
        mocker.patch("ingpipe_embedding_generation.generate_embeddings.setup_entry_logging")
        mock_gen = mocker.patch(
            "ingpipe_embedding_generation.generate_embeddings.generate_embeddings"
        )
        mocker.patch(
            "sys.argv",
            ["generate_embeddings.py", "--config", config_path, "--env-file", _empty_env(tmp_path)],
        )

        with pytest.raises(SystemExit) as excinfo:
            generate_embeddings_module.main()

        assert excinfo.value.code == 1
        mock_gen.assert_not_called()

    def test_main_no_tables_exits_one(self, mocker, tmp_path) -> None:
        """A config whose [tables] table is empty exits with code 1."""
        config_path = _write_config_text(tmp_path, self._BASE_HEADER + "[tables]\n")
        mocker.patch("ingpipe_embedding_generation.generate_embeddings.setup_entry_logging")
        mock_gen = mocker.patch(
            "ingpipe_embedding_generation.generate_embeddings.generate_embeddings"
        )
        mocker.patch(
            "sys.argv",
            ["generate_embeddings.py", "--config", config_path, "--env-file", _empty_env(tmp_path)],
        )

        with pytest.raises(SystemExit) as excinfo:
            generate_embeddings_module.main()

        assert excinfo.value.code == 1
        mock_gen.assert_not_called()

    def test_main_tables_not_a_table_exits_one(self, mocker, tmp_path) -> None:
        """`tables` given as an array (not a table of configs) exits with code 1.

        Without the shape guard this raised a bare AttributeError on .items()
        instead of taking the clean config-error path.
        """
        config_path = _write_config_text(
            tmp_path, self._BASE_HEADER + 'tables = ["a", "b"]\n'
        )
        mocker.patch("ingpipe_embedding_generation.generate_embeddings.setup_entry_logging")
        mock_gen = mocker.patch(
            "ingpipe_embedding_generation.generate_embeddings.generate_embeddings"
        )
        mocker.patch(
            "sys.argv",
            ["generate_embeddings.py", "--config", config_path, "--env-file", _empty_env(tmp_path)],
        )

        with pytest.raises(SystemExit) as excinfo:
            generate_embeddings_module.main()

        assert excinfo.value.code == 1
        mock_gen.assert_not_called()

    def test_main_table_config_not_a_table_is_skipped(self, mocker, tmp_path) -> None:
        """A per-table entry that is not a table of settings is a failed table."""
        config_path = _write_config_text(
            tmp_path,
            self._BASE_HEADER
            + "[tables]\n"
            + 'scalar_table = "oops"\n'
            + '[tables.good_table]\nembed_columns = ["content_text"]\n',
        )
        mocker.patch("ingpipe_embedding_generation.generate_embeddings.setup_entry_logging")
        mock_gen = mocker.patch(
            "ingpipe_embedding_generation.generate_embeddings.generate_embeddings", return_value=2
        )
        mocker.patch(
            "sys.argv",
            ["generate_embeddings.py", "--config", config_path, "--env-file", _empty_env(tmp_path)],
        )

        # One table failed (the scalar one) so main still exits non-zero.
        with pytest.raises(SystemExit) as excinfo:
            generate_embeddings_module.main()

        assert excinfo.value.code == 1
        # Only the valid table reached generate_embeddings.
        assert mock_gen.call_count == 1
        assert mock_gen.call_args.kwargs["source_table"] == "good_table"


class TestEnvFile:
    """Tests for the --env-file flag and the removal of the import-time load."""

    _BASE_HEADER = (
        'db_name = "policy_db"\n'
        'db_schema = "cms_iom"\n'
        'model_name = "Alibaba-NLP/gte-large-en-v1.5"\n'
    )

    def test_import_does_not_mutate_postgres_env(self, monkeypatch) -> None:
        """Importing the module leaves the four POSTGRES_* variables alone.

        The module used to call load_dotenv() at module scope, so merely
        importing it populated the process environment from whatever .env the
        working directory happened to sit above. Credentials are now resolved
        inside main() from --env-file, so a fresh import must be inert.
        """
        # Arrange: clear the four variables, then re-execute the module body.
        for var in ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD"):
            monkeypatch.delenv(var, raising=False)
        # The reload re-runs ``from ingpipe_embedding_generation._utils import
        # ...``, and three directories
        # in this repo hold a module of that bare name. In a whole-repo run a
        # sibling suite's copy is already cached and would be served instead,
        # so pin this module's directory ahead of theirs and evict the foreign
        # copy. Both changes are undone by monkeypatch at teardown, leaving
        # later suites the cache state they expect.
        monkeypatch.syspath_prepend(
            str(Path(generate_embeddings_module.__file__).resolve().parent)
        )
        monkeypatch.delitem(sys.modules, "_utils", raising=False)

        # Act
        importlib.reload(generate_embeddings_module)

        # Assert: still unset, i.e. the import read no dotenv file.
        for var in ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD"):
            assert var not in os.environ

    def test_main_missing_env_file_exits_1(self, mocker, tmp_path) -> None:
        """main() exits 1 when --env-file names a path that does not exist."""
        # Arrange: a valid config, so the exit can only come from the env file.
        config_path = _write_config_text(
            tmp_path,
            self._BASE_HEADER + '[tables.good_table]\nembed_columns = ["content_text"]\n',
        )
        missing_env = str(tmp_path / "absent" / ".env.nope")
        mocker.patch("ingpipe_embedding_generation.generate_embeddings.setup_entry_logging")
        mock_gen = mocker.patch(
            "ingpipe_embedding_generation.generate_embeddings.generate_embeddings", return_value=1
        )
        mocker.patch(
            "sys.argv",
            [
                "generate_embeddings.py",
                "--config",
                config_path,
                "--env-file",
                missing_env,
            ],
        )

        # Act
        with pytest.raises(SystemExit) as excinfo:
            generate_embeddings_module.main()

        # Assert: exited before doing any work.
        assert excinfo.value.code == 1
        assert mock_gen.call_count == 0

    def test_main_without_env_file_flag_exits_usage_error(
        self, mocker, tmp_path, capsys
    ) -> None:
        """A flag-less invocation is rejected by argparse (usage error, exit 2)."""
        # Arrange: a valid config, so the rejection can only be the missing flag.
        config_path = _write_config_text(
            tmp_path,
            self._BASE_HEADER + '[tables.good_table]\nembed_columns = ["content_text"]\n',
        )
        mock_gen = mocker.patch(
            "ingpipe_embedding_generation.generate_embeddings.generate_embeddings"
        )
        mocker.patch("sys.argv", ["generate_embeddings.py", "--config", config_path])

        # Act
        with pytest.raises(SystemExit) as excinfo:
            generate_embeddings_module.main()

        # Assert: argparse's usage error names the missing flag; no work done.
        assert excinfo.value.code == 2
        assert "--env-file" in capsys.readouterr().err
        assert mock_gen.call_count == 0
