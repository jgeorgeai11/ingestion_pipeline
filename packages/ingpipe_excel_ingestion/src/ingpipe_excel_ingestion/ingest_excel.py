"""Generic Excel-to-PostgreSQL ingestion with hybrid per-sheet routing.

Every sheet listed in the config is ALWAYS written to the consolidated
``sheet`` / ``sheet_content`` tables (the universal embedding leg): one
``sheet`` metadata row plus one ``sheet_content`` row per data row, with
``row_text`` built over the ORIGINAL headers as newline key-value. A sheet that
names a ``table`` is ADDITIONALLY written to that structured SQL table (the
text-to-SQL leg) via ``structured_table.write_rows`` — created-or-appended with
additive schema evolution.

Identity is ``collection_path`` (a sanitized-or-authored ltree, one per sheet),
matching ingpipe_file_ingestion. Overwrite / skip-if-present semantics apply per
collection_path on both legs (deleting a ``sheet`` row cascades to both legs).
Failures are accumulated per sheet (the others continue) and reported with a
non-zero exit; config-level errors abort.

Usage:
    uv run ingest-excel \
        --config instances/<instance>/config/ingpipe_excel_ingestion/<name>.toml \
        --env-file instances/<instance>/.env
"""

import sys
from pathlib import Path

from ingpipe_lib.cli import (
    build_parser,
    finish_run,
    load_config,
    run_scope,
    setup_entry_logging,
)
from ingpipe_lib.db import require_extensions
from ingpipe_lib.ddl import render_ddl_template
from ingpipe_lib.logconfig import get_logger
from ingpipe_lib.paths import InstanceRootNotFoundError, resolve_config_path

# COMMENT ON override construction is shared with the file leg
# (ingpipe_file_ingestion._utils.ensure_schema), so both emit identical statements.
from ingpipe_lib.sql_comments import COMMENT_TEXT_PARAM, build_comment_statements
from openpyxl.utils import column_index_from_string
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from ingpipe_excel_ingestion import structured_table
from ingpipe_excel_ingestion._utils import (
    compute_source_hash,
    get_engine,
    make_collection_path,
    validate_collection_path,
    validate_sql_identifier,
)
from ingpipe_excel_ingestion.excel_parser import ROW_NUMBER_KEY, parse_sheet, validate_column_letter

logger = get_logger(__name__)

# Default names of the consolidated tables. Configurable per config via the
# top-level ``sheet_table`` / ``content_table`` keys (these are the defaults used
# when those keys are omitted), mirroring ingpipe_file_ingestion's document/content_table.
SHEET_TABLE = "sheet"
CONTENT_TABLE = "sheet_content"

_REQUIRED_TOP_LEVEL = ("source_dir", "db_name", "db_schema", "files")
_REQUIRED_SHEET_FIELDS = ("sheet",)
_DEFAULT_OVERWRITE = False
_DEFAULT_MIN_COLUMN_OVERLAP = 0.5


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def _require_str(value: object, label: str) -> None:
    """Raise ValueError unless ``value`` is a string.

    Guards the config fields that are later used as SQL identifiers or paths.
    Both consumers raise ``TypeError`` for a non-string —
    ``validate_sql_identifier``'s regex ``fullmatch`` and ``Path()`` — which
    escapes ``main()``'s ``except ValueError`` abort and surfaces as an unhandled
    traceback, so the type is asserted here where it is still a config error.

    Args:
        value: The config value to check.
        label: Field name (or dotted config path) used in the error message.

    Raises:
        ValueError: If ``value`` is not a ``str``.
    """
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string, got {value!r}")


def validate_config(config: dict) -> None:
    """Validate config structure and identifier / bound / path safety.

    Checks the required top-level fields (``source_dir``/``db_name``/
    ``db_schema``/``files``); that ``source_dir``/``db_name`` are strings and any
    ``overwrite`` is a boolean; that ``db_schema`` and the optional consolidated
    table names (``sheet_table``/``content_table``) are strings and safe SQL
    identifiers; the ``min_column_overlap`` range; that each file entry is a
    table with a ``sheets`` list; and per sheet that ``sheet`` is present (the
    only required per-sheet field), that any provided
    ``header_row``/``data_start_row``/``data_end_row`` are integers of at
    least 1 (row numbers are 1-based) with sane relationships when both of a
    pair are given (``data_start_row > header_row``,
    ``data_end_row >= data_start_row``), that any ``start_col``/``end_col`` are
    Excel column letters (``start_col`` no later than ``end_col``), that any
    per-sheet ``table`` is a string and a safe SQL identifier, and that any
    authored ``collection_path`` is a valid lowercase ltree; and that any
    top-level ``collection_path_prefix`` is a string and a valid lowercase
    ltree on its own. Defaults for omitted bounds are resolved and fully
    re-validated by the parser.

    Values are type-checked here so that a wrong-typed TOML value is a clean
    config-level abort: downstream, a non-string raises ``TypeError`` (which
    ``main()`` does not catch) and a non-boolean ``overwrite`` does not raise at
    all — a quoted ``"false"`` is a truthy string and would overwrite silently.

    Comment keys (COMMENT ON text applied at ingest): the optional top-level
    ``schema_comment``/``sheet_table_comment``/``content_table_comment`` and the
    optional per-sheet ``table_comment`` must be strings; a per-sheet
    ``table_comment`` without a ``table`` is rejected (there is no structured
    table to describe).

    Args:
        config: Parsed TOML config dict.

    Raises:
        ValueError: If any required field is missing, a value has the wrong type,
            a bound/column relationship is invalid, an identifier is unsafe, or an
            authored collection_path is invalid (all config-level errors that
            should abort).
    """
    for field in _REQUIRED_TOP_LEVEL:
        if field not in config:
            raise ValueError(f"Missing required config field: {field!r}")

    # The two required scalars that are consumed outside any ValueError handler:
    # source_dir reaches Path() and db_name reaches get_engine().
    _require_str(config["source_dir"], "source_dir")
    _require_str(config["db_name"], "db_name")

    # overwrite is the one key whose wrong type changes behaviour silently rather
    # than raising: a quoted `overwrite = "false"` is a truthy string, so the run
    # would DELETE and re-write every present sheet instead of skipping it.
    if "overwrite" in config and not isinstance(config["overwrite"], bool):
        raise ValueError(
            f"overwrite must be a boolean, got {config['overwrite']!r}"
        )

    _require_str(config["db_schema"], "db_schema")
    validate_sql_identifier(config["db_schema"], "db_schema")

    # Optional consolidated table names default to SHEET_TABLE/CONTENT_TABLE; a
    # custom value is rendered into the DDL and insert/select SQL, so it must be a
    # safe SQL identifier (same guard as db_schema and the per-sheet table).
    sheet_table = config.get("sheet_table", SHEET_TABLE)
    content_table = config.get("content_table", CONTENT_TABLE)
    _require_str(sheet_table, "sheet_table")
    _require_str(content_table, "content_table")
    validate_sql_identifier(sheet_table, "sheet_table")
    validate_sql_identifier(content_table, "content_table")
    # Equal names would make the second `create table if not exists` a silent
    # no-op (the content table would never get its columns), surfacing later as
    # an opaque per-sheet insert error; reject it as a clean config error here.
    if sheet_table == content_table:
        raise ValueError(
            f"sheet_table and content_table must differ (both = {sheet_table!r})"
        )

    # Optional COMMENT ON texts (schema + the two consolidated tables). The text
    # is data (bound as a parameter, not an identifier), but a wrong-typed value
    # would only surface as an opaque driver error mid-run; abort cleanly here.
    for comment_key in (
        "schema_comment", "sheet_table_comment", "content_table_comment"
    ):
        comment_value = config.get(comment_key)
        if comment_value is not None and not isinstance(comment_value, str):
            raise ValueError(
                f"{comment_key} must be a string, got {comment_value!r}"
            )

    # collection_path_prefix (optional) is prepended to every DERIVED sheet path
    # (never to an authored override). It is validated as an ltree here, once,
    # rather than being discovered to be malformed once per sheet -- and because
    # both the ingester and the output validator re-derive every path from it, a
    # bad prefix must abort at config load in BOTH, not produce two different
    # wrong answers.
    prefix = config.get("collection_path_prefix")
    if prefix is not None:
        if not isinstance(prefix, str):
            raise ValueError(
                f"collection_path_prefix must be a string, got {prefix!r}"
            )
        validate_collection_path(prefix)

    # min_column_overlap (optional) flows into the structured-leg overlap guard as
    # a float comparison; a non-numeric value would raise TypeError deep in
    # write_rows (escaping main()'s ValueError abort), so validate it here.
    min_overlap = config.get("min_column_overlap")
    if min_overlap is not None and (
        isinstance(min_overlap, bool)
        or not isinstance(min_overlap, (int, float))
        or not 0.0 <= min_overlap <= 1.0
    ):
        raise ValueError(
            f"min_column_overlap must be a number in [0, 1], got {min_overlap!r}"
        )

    files = config["files"]
    if not isinstance(files, dict):
        raise ValueError(
            f"'files' must be a table of workbooks, got {type(files).__name__}"
        )

    for filename, file_entry in files.items():
        if not isinstance(file_entry, dict):
            raise ValueError(
                f"files[{filename!r}] must be a table, got "
                f"{type(file_entry).__name__}"
            )
        if "sheets" not in file_entry:
            raise ValueError(
                f"files[{filename!r}]: missing required field 'sheets'"
            )
        sheets = file_entry["sheets"]
        if not isinstance(sheets, list):
            raise ValueError(
                f"files[{filename!r}].sheets must be a list, got "
                f"{type(sheets).__name__}"
            )
        for j, sheet_entry in enumerate(sheets):
            if not isinstance(sheet_entry, dict):
                raise ValueError(
                    f"files[{filename!r}].sheets[{j}] must be a table, got "
                    f"{type(sheet_entry).__name__}"
                )
            missing = [f for f in _REQUIRED_SHEET_FIELDS if f not in sheet_entry]
            if missing:
                raise ValueError(
                    f"files[{filename!r}].sheets[{j}]: missing required "
                    f"fields {missing}"
                )

            # Optional row bounds: must be ints when present (a quoted "5" would
            # otherwise raise TypeError outside the ValueError abort path).
            for row_field in ("header_row", "data_start_row", "data_end_row"):
                if row_field in sheet_entry:
                    value = sheet_entry[row_field]
                    if isinstance(value, bool) or not isinstance(value, int):
                        raise ValueError(
                            f"files[{filename!r}].sheets[{j}]: {row_field} must "
                            f"be an integer, got {value!r}"
                        )
                    # Row numbers are 1-based; 0 or a negative would reach the
                    # parser as a Python negative index and silently read the
                    # WRONG row (e.g. header_row = 0 -> the sheet's last row).
                    if value < 1:
                        raise ValueError(
                            f"files[{filename!r}].sheets[{j}]: {row_field} must "
                            f"be >= 1 (row numbers are 1-based), got {value}"
                        )

            # Bound relationships are checked only for explicitly-provided pairs;
            # the parser resolves defaults and fully re-validates the result.
            header_row = sheet_entry.get("header_row")
            data_start_row = sheet_entry.get("data_start_row")
            data_end_row = sheet_entry.get("data_end_row")
            if (
                header_row is not None
                and data_start_row is not None
                and data_start_row <= header_row
            ):
                raise ValueError(
                    f"files[{filename!r}].sheets[{j}]: data_start_row "
                    f"({data_start_row}) must be greater than header_row "
                    f"({header_row})"
                )
            if (
                data_start_row is not None
                and data_end_row is not None
                and data_end_row < data_start_row
            ):
                raise ValueError(
                    f"files[{filename!r}].sheets[{j}]: data_end_row "
                    f"({data_end_row}) must be >= data_start_row "
                    f"({data_start_row})"
                )

            # Optional column letters (Excel labels like 'A', 'B', 'AA').
            for col_field in ("start_col", "end_col"):
                if col_field in sheet_entry:
                    validate_column_letter(sheet_entry[col_field], col_field)
            start_col = sheet_entry.get("start_col")
            end_col = sheet_entry.get("end_col")
            if (
                start_col is not None
                and end_col is not None
                and column_index_from_string(end_col.upper())
                < column_index_from_string(start_col.upper())
            ):
                raise ValueError(
                    f"files[{filename!r}].sheets[{j}]: end_col ({end_col}) must "
                    f"be at or after start_col ({start_col})"
                )

            table = sheet_entry.get("table")
            if table is not None:
                _require_str(table, f"files[{filename!r}].sheets[{j}]: table")
                validate_sql_identifier(table, "table")

            # A structured-table description only makes sense alongside a
            # `table`; a stray table_comment is a config mistake, not a no-op.
            table_comment = sheet_entry.get("table_comment")
            if table_comment is not None:
                if table is None:
                    raise ValueError(
                        f"files[{filename!r}].sheets[{j}]: table_comment "
                        "requires a table"
                    )
                if not isinstance(table_comment, str):
                    raise ValueError(
                        f"files[{filename!r}].sheets[{j}]: table_comment must "
                        f"be a string, got {table_comment!r}"
                    )

            # An authored collection_path must be a valid ltree; fail fast here
            # (derived paths are validated when resolved during the run).
            override = sheet_entry.get("collection_path")
            if override is not None:
                validate_collection_path(override)


# ---------------------------------------------------------------------------
# Consolidated (embedding) leg
# ---------------------------------------------------------------------------


def ensure_consolidated_tables(
    engine: Engine,
    db_schema: str,
    sheet_table: str = SHEET_TABLE,
    content_table: str = CONTENT_TABLE,
    *,
    schema_comment: str | None = None,
    sheet_table_comment: str | None = None,
    content_table_comment: str | None = None,
) -> None:
    """Create the consolidated sheet / sheet_content tables if absent.

    Reads the DDL template, substitutes the validated schema/table identifiers,
    and executes it. The template provisions the schema and uses IF NOT EXISTS
    throughout, so this is safe to call repeatedly. The required ltree
    extension is NOT created here (provisioning owns extension installs); the
    caller verifies it via ``ingpipe_lib.db.require_extensions`` first. The
    sheet/content table names are configurable; the content table's FK references
    ``sheet_table``, so the same name must feed this function and the structured
    leg (``structured_table.write_rows``).

    When a comment override is provided it is applied with COMMENT ON after the
    template runs (same transaction), overriding the template's generic table
    comments. Application is unconditional on every call (COMMENT ON
    overwrites), so a re-run refreshes the text; a None override leaves the
    existing comment alone. Comment text is data, bound as a parameter so the
    driver quotes it safely.

    Args:
        engine: SQLAlchemy engine.
        db_schema: Target schema (validated).
        sheet_table: Name of the consolidated sheet table. Defaults to "sheet".
        content_table: Name of the consolidated content table. Defaults to
            "sheet_content".
        schema_comment: Optional COMMENT ON SCHEMA text.
        sheet_table_comment: Optional COMMENT ON TABLE text for the sheet table.
        content_table_comment: Optional COMMENT ON TABLE text for the content
            table.

    Raises:
        FileNotFoundError: If the DDL template is missing.
        ValueError: If a table identifier is unsafe, or the rendered DDL still
            contains an unsubstituted placeholder.
    """
    validate_sql_identifier(db_schema, "db_schema")
    validate_sql_identifier(sheet_table, "sheet_table")
    validate_sql_identifier(content_table, "content_table")
    ddl_path = Path(__file__).parent / "sql" / "excel_schema.sql"

    # Shared read-substitute-verify: raises FileNotFoundError for a missing
    # template and ValueError for any surviving {placeholder} (template/code
    # drift), both logged by the lib helper.
    rendered = render_ddl_template(
        ddl_path,
        {
            "schema_name": db_schema,
            "sheet_table": sheet_table,
            "content_table": content_table,
        },
    )

    # Config-supplied comment overrides, applied after the template so they win
    # over its generic table comments. Built by the shared ingpipe_lib helper (see
    # it for the psycopg2 client-side binding the :comment_text parameter
    # depends on) so both ingestion legs emit identical statements.
    comment_statements = build_comment_statements(
        db_schema,
        schema_comment=schema_comment,
        table_comments={
            sheet_table: sheet_table_comment,
            content_table: content_table_comment,
        },
    )

    with engine.begin() as conn:
        conn.execute(text(rendered))
        for statement, comment_text in comment_statements:
            conn.execute(text(statement), {COMMENT_TEXT_PARAM: comment_text})
    logger.info(
        f"Consolidated tables ensured: {db_schema}.{sheet_table}, "
        f"{db_schema}.{content_table} "
        f"({len(comment_statements)} comment override(s) applied)"
    )


def build_row_text(column_names: list[str], row: dict[str, str | int | None]) -> str:
    """Build the newline key-value ``row_text`` over ORIGINAL headers for a row.

    Format: ``"Header A: value\\nHeader B: value\\n..."`` — one ``col: value``
    per line (plain newline key-value, the format research favours for tabular
    RAG: column names give the embedding semantic context, in the token-efficient
    layout LLMs are trained on). Only ``None`` (a missing or blank cell) renders
    as empty; a falsy-but-present value such as ``0`` or ``""`` renders verbatim.
    The synthetic ``row_number`` is never included (it is not a header).

    Args:
        column_names: Original Excel headers, in sheet order.
        row: A parsed row dict (header-keyed, plus ``ROW_NUMBER_KEY``).

    Returns:
        The newline-joined key-value row text.
    """
    # Test `is None` rather than falsiness: a truthiness test would collapse an
    # int 0 (permitted by the signature) to empty and bake that loss into both
    # row_text and the source_binary_hash computed over it.
    parts = [
        f"{col}: {'' if row.get(col) is None else row.get(col)}"
        for col in column_names
    ]
    return "\n".join(parts)


def _content_source_present(
    conn: Connection, db_schema: str, sheet_table: str, collection_path: str
) -> bool:
    """Return whether a sheet metadata row already exists for the path.

    Args:
        conn: Active SQLAlchemy connection.
        db_schema: Target schema (validated by the caller).
        sheet_table: Name of the consolidated sheet table (validated upstream).
        collection_path: The sheet's ltree identity to check for.

    Returns:
        True if a sheet metadata row exists for ``collection_path``.
    """
    result = conn.execute(
        text(
            f"select 1 from {db_schema}.{sheet_table} "
            "where collection_path = :cp"
        ),
        {"cp": collection_path},
    )
    return result.fetchone() is not None


def write_consolidated(
    engine: Engine,
    db_schema: str,
    collection_path: str,
    title: str,
    structured_table_name: str | None,
    column_names: list[str],
    rows: list[dict[str, str | int | None]],
    *,
    overwrite: bool,
    sheet_table: str = SHEET_TABLE,
    content_table: str = CONTENT_TABLE,
) -> int:
    """Write a sheet to the sheet / sheet_content leg in one transaction.

    If ``overwrite``, the existing ``sheet`` row is deleted first (cascading to
    its content rows and any structured rows) and re-inserted. If not
    ``overwrite`` and the source is already present, the write is skipped
    (returns ``-1``) WITHOUT re-inserting the metadata row. The ``sheet`` row
    carries the per-sheet ``source_binary_hash`` (content fingerprint).

    Precondition: ``rows`` must be non-empty. The ``sheet`` row stores
    ``n_rows = len(rows)`` and its DDL constrains ``check (n_rows >= 1)``
    (``sql/excel_schema.sql``), so an empty list cannot be written — callers must
    skip a zero-row sheet instead (``main()`` does, before calling).

    Args:
        engine: SQLAlchemy engine.
        db_schema: Target schema (validated by the caller).
        collection_path: The sheet's ltree identity.
        title: Human-readable sheet title (defaults upstream to
            "<workbook-stem> — <sheet>").
        structured_table_name: Structured table this sheet also feeds, or None.
        column_names: Original Excel headers.
        rows: Parsed row dicts. Must be non-empty (see the precondition above).
        overwrite: If True, replace this source; else skip if present.
        sheet_table: Name of the consolidated sheet table. Defaults to "sheet".
        content_table: Name of the consolidated content table. Defaults to
            "sheet_content".

    Returns:
        The number of content rows written, or ``-1`` if skipped.

    Raises:
        SQLAlchemyError: If any statement in the transaction fails (the whole
            sheet is rolled back), including the ``n_rows >= 1`` CHECK violation
            raised when ``rows`` is empty.
    """
    row_texts = [build_row_text(column_names, row) for row in rows]
    source_hash = compute_source_hash(row_texts)

    with engine.begin() as conn:
        present = _content_source_present(
            conn, db_schema, sheet_table, collection_path
        )
        if present and not overwrite:
            logger.info(
                f"Source {collection_path!r} already in {db_schema}.{sheet_table} "
                "and overwrite=false; skipping"
            )
            return -1
        if present and overwrite:
            conn.execute(
                text(
                    f"delete from {db_schema}.{sheet_table} "
                    "where collection_path = :cp"
                ),
                {"cp": collection_path},
            )

        conn.execute(
            text(
                f"insert into {db_schema}.{sheet_table} "
                "(collection_path, title, n_rows, source_binary_hash, "
                "structured_table) "
                "values (:cp, :title, :n_rows, :hash, :structured_table)"
            ),
            {
                "cp": collection_path,
                "title": title,
                "n_rows": len(rows),
                "hash": source_hash,
                "structured_table": structured_table_name,
            },
        )

        content_params = [
            {
                "cp": collection_path,
                "sort_order": row[ROW_NUMBER_KEY],
                "row_text": row_text,
                "word_count": len(row_text.split()),
            }
            for row, row_text in zip(rows, row_texts)
        ]
        conn.execute(
            text(
                f"insert into {db_schema}.{content_table} "
                "(collection_path, sort_order, row_text, word_count) "
                "values (:cp, :sort_order, :row_text, :word_count)"
            ),
            content_params,
        )

    logger.info(
        f"Embedded: {collection_path} -> {db_schema}.{content_table}: "
        f"{len(rows)} rows"
    )
    return len(rows)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point: parse config, ingest each sheet, report resiliently."""
    # Canonical --config/--env-file pair plus this script's --overwrite.
    parser = build_parser(
        "Ingest Excel files into PostgreSQL (hybrid storage model)"
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        default=None,
        help="Delete and re-ingest sheets already present (overrides TOML config)",
    )
    args = parser.parse_args()

    # INFO level, named from the config stem, anchored to the instance root.
    config_path = Path(args.config)
    setup_entry_logging("ingpipe_excel_ingestion", config_path)

    with run_scope():
        # Resolve credentials and load the TOML config (exits 1 on a missing
        # env file, missing config, or malformed TOML).
        config = load_config(config_path, args.env_file)

        # Config-level errors abort.
        try:
            validate_config(config)
        except ValueError as e:
            logger.error(f"Config validation failed: {e}")
            sys.exit(1)

        # Anchor a relative source_dir to the instance root, never the CWD:
        # an installed console script runs from anywhere.
        try:
            source_dir = str(resolve_config_path(config["source_dir"], config_path))
        except InstanceRootNotFoundError:
            # Already logged with the config path by require_instance_root.
            sys.exit(1)
        files = config["files"]
        db_name = config["db_name"]
        db_schema = config["db_schema"]
        # Consolidated table names default to the current sheet/sheet_content
        # values; validate_config has already checked these are safe SQL
        # identifiers. The same sheet_table name feeds the DDL (which creates
        # it) and the structured leg's FK (which references it), so read it
        # once and thread it through both.
        sheet_table = config.get("sheet_table", SHEET_TABLE)
        content_table = config.get("content_table", CONTENT_TABLE)
        overwrite = config.get("overwrite", _DEFAULT_OVERWRITE)
        # The CLI flag overrides the TOML value (same precedence as the other
        # two ingesters); absent means "use the config". The TOML value's
        # boolean type is enforced by validate_config above.
        if args.overwrite is not None:
            overwrite = args.overwrite
        min_column_overlap = config.get(
            "min_column_overlap", _DEFAULT_MIN_COLUMN_OVERLAP
        )

        logger.info(
            f"Config loaded: {len(files)} file(s), db={db_name}, schema={db_schema}, "
            f"tables={sheet_table}/{content_table}, "
            f"overwrite={overwrite}, min_column_overlap={min_column_overlap}"
        )

        try:
            engine = get_engine(db_name)
            # Extension contract: provisioning installs ltree, the engine only
            # verifies it — failing here, before any DDL, with an actionable
            # message rather than a privilege error mid-transaction.
            require_extensions(engine, ["ltree"])
            ensure_consolidated_tables(
                engine, db_schema, sheet_table, content_table,
                schema_comment=config.get("schema_comment"),
                sheet_table_comment=config.get("sheet_table_comment"),
                content_table_comment=config.get("content_table_comment"),
            )
        except (ValueError, FileNotFoundError, SQLAlchemyError) as e:
            logger.error(f"Setup failed: {e}")
            sys.exit(1)

        embedded = 0
        skipped = 0
        structured = 0
        total_sheets = 0
        failures: list[dict[str, str]] = []

        for filename, file_entry in files.items():
            filepath = Path(source_dir) / filename
            logger.info(f"Processing file: {filename}")

            for sheet_entry in file_entry["sheets"]:
                sheet_name = sheet_entry["sheet"]
                table_name = sheet_entry.get("table")
                total_sheets += 1

                # Parse leg.
                try:
                    column_names, rows = parse_sheet(
                        filepath,
                        sheet_name,
                        header_row=sheet_entry.get("header_row"),
                        data_start_row=sheet_entry.get("data_start_row"),
                        data_end_row=sheet_entry.get("data_end_row"),
                        start_col=sheet_entry.get("start_col"),
                        end_col=sheet_entry.get("end_col"),
                    )
                except (FileNotFoundError, ValueError) as e:
                    logger.error(f"Parse failed for {filename}:{sheet_name}: {e}")
                    failures.append(
                        {
                            "file": filename,
                            "sheet": sheet_name,
                            "stage": "parse",
                            "reason": str(e),
                        }
                    )
                    continue

                # Skip a sheet whose configured range yields no data rows:
                # writing a sheet metadata row with no sheet_content children
                # is the very state the output validator (and the n_rows >= 1
                # CHECK) rejects.
                if not rows:
                    logger.warning(
                        f"No data rows parsed in {filename}:{sheet_name}; skipping"
                    )
                    skipped += 1
                    continue

                # Resolve the sheet's collection_path (derived from
                # filename.sheet, or the config-authored override). A
                # degenerate name fails this sheet.
                try:
                    collection_path = make_collection_path(
                        filename,
                        sheet_name,
                        sheet_entry.get("collection_path"),
                        config.get("collection_path_prefix"),
                    )
                except ValueError as e:
                    logger.error(
                        f"collection_path resolution failed for "
                        f"{filename}:{sheet_name}: {e}"
                    )
                    failures.append(
                        {
                            "file": filename,
                            "sheet": sheet_name,
                            "stage": "collection_path",
                            "reason": str(e),
                        }
                    )
                    continue

                # Default the title to "<workbook-stem> — <sheet>" so it
                # stays unambiguous when many workbooks share a sheet name
                # (e.g. qpp_cm's 22 "Triggers" sheets); an authored title
                # overrides it.
                title = sheet_entry.get("title")
                if title is None:
                    title = f"{Path(filename).stem} — {sheet_name}"

                # Embedding leg (always). A failure here is recorded but does
                # not stop other sheets.
                try:
                    marker = write_consolidated(
                        engine, db_schema, collection_path, title, table_name,
                        column_names, rows, overwrite=overwrite,
                        sheet_table=sheet_table, content_table=content_table,
                    )
                    if marker >= 0:
                        embedded += 1
                    else:
                        # -1 = already present and overwrite=false. Count it
                        # so an idempotent re-run (the standard
                        # comment-refresh sweep) reports "skipped N" instead
                        # of reading as "embedded 0/N".
                        skipped += 1
                except SQLAlchemyError as e:
                    logger.error(f"Embed failed for {filename}:{sheet_name}: {e}")
                    failures.append(
                        {
                            "file": filename,
                            "sheet": sheet_name,
                            "stage": "embed",
                            "reason": str(e),
                        }
                    )
                    # If the embed leg failed, do not attempt the structured
                    # leg (its FK to the sheet row would fail anyway).
                    continue

                # Structured leg (only when a table is named).
                if table_name is not None:
                    try:
                        s_marker = structured_table.write_rows(
                            engine, db_schema, table_name, sheet_table,
                            collection_path, column_names, rows,
                            overwrite=overwrite,
                            min_column_overlap=min_column_overlap,
                            table_comment=sheet_entry.get("table_comment"),
                        )
                        if s_marker >= 0:
                            structured += 1
                    except (ValueError, SQLAlchemyError) as e:
                        logger.error(
                            f"Structured write failed for {filename}:{sheet_name} "
                            f"-> {table_name}: {e}"
                        )
                        failures.append(
                            {
                                "file": filename,
                                "sheet": sheet_name,
                                "stage": "structured",
                                "reason": str(e),
                            }
                        )

        logger.info(
            f"Summary: embedded {embedded}, skipped {skipped}, "
            f"structured {structured} of {total_sheets} sheet(s); "
            f"{len(failures)} failure(s)"
        )
        finish_run(
            [
                f"{failure['file']}:{failure['sheet']} "
                f"[{failure['stage']}] {failure['reason']}"
                for failure in failures
            ],
            success_message=f"SUCCESS: ingest complete for {total_sheets} sheet(s)",
            failure_prefix="FAILURE",
        )


if __name__ == "__main__":
    main()
