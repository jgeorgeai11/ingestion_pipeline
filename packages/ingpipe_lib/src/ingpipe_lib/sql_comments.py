"""Shared builder for config-authored COMMENT ON overrides.

Both ingestion legs let a corpus config override the generic descriptions
baked into their DDL templates: ``ingpipe_file_ingestion._utils.ensure_schema`` and
``ingpipe_excel_ingestion.ingest_excel.ensure_consolidated_tables`` each apply a
schema comment plus one comment per consolidated table. The construction of
those statements is identical on both legs, so it is kept here once
rather than kept as two copies that drift (the copies had already diverged in
error handling before this extraction).

Provides:
  - ``build_comment_statements``: the ordered ``(statement, comment_text)``
    pairs for the non-None overrides.
  - ``COMMENT_TEXT_PARAM``: the bind-parameter name used by those statements,
    so callers build the bind dict from the same constant the SQL uses.

Statement construction only: the caller owns transaction scope, execution, and
error handling, because the two legs wrap execution differently.
"""

from ingpipe_lib.validators import validate_sql_identifier

__all__ = ["COMMENT_TEXT_PARAM", "build_comment_statements"]

# Comment text is data, not an identifier, so it is bound rather than inlined
# as a SQL literal. PostgreSQL rejects server-side bind parameters in COMMENT
# ON, but psycopg2 interpolates client-side, so the driver quotes the text
# safely (single quotes, ampersands) with no hand-rolled literal escaping.
# A driver that binds server-side (psycopg3's default) would reject these.
COMMENT_TEXT_PARAM = "comment_text"


def build_comment_statements(
    db_schema: str,
    *,
    schema_comment: str | None = None,
    table_comments: dict[str, str | None] | None = None,
) -> list[tuple[str, str]]:
    """Build the COMMENT ON statements for every override that is not None.

    Identifiers are interpolated into the statement text (COMMENT ON takes no
    parameter there), so each one is validated first as an injection guard —
    the callers validate the same names earlier, and this keeps the guard with
    the interpolation. The comment text itself is returned alongside the
    statement for the caller to bind under ``COMMENT_TEXT_PARAM``.

    Statement order is the schema comment first, then the table comments in
    ``table_comments`` insertion order, so a caller's execution order is
    predictable and testable.

    Args:
        db_schema: Target schema name (validated as a safe SQL identifier).
        schema_comment: Optional COMMENT ON SCHEMA text. None emits no
            statement, leaving any existing schema description untouched.
        table_comments: Mapping of table name to its optional COMMENT ON TABLE
            text. Names are validated as safe SQL identifiers even when their
            text is None; a None text emits no statement. A repeated table
            name collapses to one entry (dict semantics).

    Returns:
        A list of ``(statement, comment_text)`` pairs, where each statement
        binds its text as ``:comment_text``. Empty when every override is None.

    Raises:
        ValueError: If ``db_schema`` or any ``table_comments`` key is not a
            safe SQL identifier.
    """
    validate_sql_identifier(db_schema, "db_schema")

    statements: list[tuple[str, str]] = []
    if schema_comment is not None:
        statements.append(
            (
                f"comment on schema {db_schema} is :{COMMENT_TEXT_PARAM}",
                schema_comment,
            )
        )

    for table_name, comment_text in (table_comments or {}).items():
        validate_sql_identifier(table_name, "table_comments key")
        if comment_text is None:
            continue
        statements.append(
            (
                f"comment on table {db_schema}.{table_name} is :{COMMENT_TEXT_PARAM}",
                comment_text,
            )
        )

    return statements
