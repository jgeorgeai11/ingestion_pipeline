"""Unit tests for the COMMENT ON builder (ingpipe_lib.sql_comments)."""

import pytest
from ingpipe_lib.sql_comments import COMMENT_TEXT_PARAM, build_comment_statements


def test_build_comment_statements_no_overrides_returns_empty() -> None:
    """With every override omitted, no statement is produced."""
    assert build_comment_statements("rag") == []


def test_build_comment_statements_all_none_returns_empty() -> None:
    """Explicit None overrides emit nothing, leaving existing descriptions."""
    statements = build_comment_statements(
        "rag",
        schema_comment=None,
        table_comments={"doc": None, "doc_content": None},
    )

    assert statements == []


def test_build_comment_statements_orders_schema_then_tables() -> None:
    """The schema statement comes first, then tables in insertion order."""
    statements = build_comment_statements(
        "rag",
        schema_comment="Schema text",
        table_comments={"doc": "Doc text", "doc_content": "Content text"},
    )

    assert statements == [
        ("comment on schema rag is :comment_text", "Schema text"),
        ("comment on table rag.doc is :comment_text", "Doc text"),
        ("comment on table rag.doc_content is :comment_text", "Content text"),
    ]


def test_build_comment_statements_skips_none_table_among_set_ones() -> None:
    """A None table text is skipped while its siblings still emit."""
    statements = build_comment_statements(
        "rag",
        table_comments={"doc": None, "doc_content": "Content text"},
    )

    assert statements == [
        ("comment on table rag.doc_content is :comment_text", "Content text"),
    ]


def test_build_comment_statements_binds_text_verbatim() -> None:
    """Text with quotes, ampersands, and non-ASCII is returned unescaped.

    Escaping is the driver's job via the bound parameter; any hand-rolled
    quoting here would double-escape it.
    """
    comment_text = "It's the MIF table — codes & lookups"

    statements = build_comment_statements("rag", schema_comment=comment_text)

    assert statements[0][1] == comment_text


def test_build_comment_statements_param_name_matches_statement() -> None:
    """The exported bind-parameter name is the one the statement references."""
    statements = build_comment_statements("rag", schema_comment="Schema text")

    assert statements[0][0].endswith(f":{COMMENT_TEXT_PARAM}")


def test_build_comment_statements_unsafe_schema_raises() -> None:
    """An unsafe schema name is rejected rather than interpolated into SQL."""
    with pytest.raises(ValueError, match="Unsafe SQL identifier"):
        build_comment_statements("bad-schema", schema_comment="Schema text")


@pytest.mark.parametrize("table_name", ["Bad-Doc", "bad content", "doc;drop", ""])
def test_build_comment_statements_unsafe_table_raises(table_name: str) -> None:
    """An unsafe table name is rejected rather than interpolated into SQL."""
    with pytest.raises(ValueError, match="Unsafe SQL identifier"):
        build_comment_statements("rag", table_comments={table_name: "Doc text"})


def test_build_comment_statements_unsafe_table_with_none_text_still_raises() -> None:
    """Key validation is unconditional, so a bad name fails fast even if unused."""
    with pytest.raises(ValueError, match="Unsafe SQL identifier"):
        build_comment_statements("rag", table_comments={"Bad-Doc": None})
