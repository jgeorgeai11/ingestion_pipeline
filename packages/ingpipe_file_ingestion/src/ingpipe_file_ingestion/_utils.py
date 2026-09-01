"""Shared utilities for file ingestion scripts."""

from pathlib import Path

from ingpipe_lib.ddl import render_ddl_template
from ingpipe_lib.logconfig import get_logger

# COMMENT ON override construction is shared with the excel leg
# (ingest_excel.ensure_consolidated_tables), so both emit identical statements.
from ingpipe_lib.sql_comments import COMMENT_TEXT_PARAM, build_comment_statements

# Re-exported so ingpipe_file_ingestion's call sites and tests keep importing these
# names from _utils; the canonical copies live in ingpipe_lib.validators.
from ingpipe_lib.validators import validate_collection_path, validate_sql_identifier
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

__all__ = ["ensure_schema", "validate_collection_path", "validate_sql_identifier"]

logger = get_logger(__name__)


def ensure_schema(
    engine: Engine,
    db_schema: str,
    ddl_path: Path,
    document_table: str = "document",
    content_table: str = "document_content",
    *,
    schema_comment: str | None = None,
    document_table_comment: str | None = None,
    content_table_comment: str | None = None,
) -> None:
    """Create the database schema and tables if they do not already exist.

    Reads the DDL template, replaces the {schema_name}, {document_table},
    and {content_table} placeholders with validated identifiers, and
    executes the DDL. All statements use IF NOT EXISTS so this is safe
    to call repeatedly.

    When a comment override is provided it is applied with COMMENT ON after
    the template runs (in the same transaction), so a config-authored,
    corpus-flavored description wins over the template's generic table
    comments. Comment application is unconditional on every call — COMMENT ON
    overwrites — so a re-run refreshes the text even when the tables already
    exist. A None override leaves the existing comment untouched (the
    template's generic table comments still apply). Comment text is data, not
    an identifier: it is passed as a bound parameter so the driver quotes it
    safely (single quotes, ampersands, etc.).

    Args:
        engine: SQLAlchemy Engine connected to the target database.
        db_schema: PostgreSQL schema name (validated as a safe SQL identifier).
        ddl_path: Path to the DDL template file.
        document_table: Name of the document table. Defaults to "document".
        content_table: Name of the content table. Defaults to "document_content".
        schema_comment: Optional COMMENT ON SCHEMA text. None leaves the
            existing schema comment alone.
        document_table_comment: Optional COMMENT ON TABLE text for the
            document table, overriding the template's generic comment.
        content_table_comment: Optional COMMENT ON TABLE text for the
            content table, overriding the template's generic comment.

    Raises:
        FileNotFoundError: If ddl_path does not exist.
        ValueError: If db_schema or table names are not safe SQL identifiers,
            or if the rendered DDL still contains an unsubstituted placeholder.
        OSError: If the DDL template cannot be read.
        SQLAlchemyError: If executing the rendered DDL fails.
    """
    logger.debug(f"Ensuring schema {db_schema} from {ddl_path}")

    validate_sql_identifier(db_schema, "db_schema")
    validate_sql_identifier(document_table, "document_table")
    validate_sql_identifier(content_table, "content_table")

    # Shared read-substitute-verify: raises FileNotFoundError for a missing
    # template and ValueError for any surviving {placeholder} (template/code
    # drift), both logged by the lib helper.
    rendered_sql = render_ddl_template(
        ddl_path,
        {
            "schema_name": db_schema,
            "document_table": document_table,
            "content_table": content_table,
        },
    )

    # Config-supplied comment overrides, applied after the template so they
    # win over its generic table comments. Built by the shared ingpipe_lib helper
    # (see it for the psycopg2 client-side binding the :comment_text parameter
    # depends on) so both ingestion legs emit identical statements.
    comment_statements = build_comment_statements(
        db_schema,
        schema_comment=schema_comment,
        table_comments={
            document_table: document_table_comment,
            content_table: content_table_comment,
        },
    )

    try:
        with engine.begin() as conn:
            conn.execute(text(rendered_sql))
            for statement, comment_text in comment_statements:
                conn.execute(text(statement), {COMMENT_TEXT_PARAM: comment_text})
    except SQLAlchemyError as e:
        logger.error(f"Failed to execute DDL for schema {db_schema}: {e}")
        raise
    else:
        logger.debug(
            f"DDL executed successfully for schema {db_schema} "
            f"({len(comment_statements)} comment override(s) applied)"
        )

    logger.info(f"Schema ensured: {db_schema} (from {ddl_path.name})")
