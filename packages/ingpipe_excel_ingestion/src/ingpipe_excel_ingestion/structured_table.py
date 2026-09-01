"""Structured-table create-or-append engine for the Excel ingestion module.

A structured table is the SQL / text-to-SQL leg of the hybrid model: a sheet
that names a ``table`` is written into a table of that name in addition to the
universal ``sheet_content`` embedding leg. Tables are created on first write and
evolved additively (ADD COLUMN only) on subsequent writes so multiple sheets /
workbooks accumulate into one shared table.

Identity is ``collection_path`` (the sheet's ltree key) plus a 1-based
``sort_order`` ordinal: PK ``(collection_path, sort_order)``, matching
``sheet_content`` / ``document_content``. ``collection_path`` carries a foreign
key onto the parent ``sheet`` table with ``on delete cascade`` — every sheet is
embedded, so every structured row's collection_path exists in ``sheet``, and
deleting a sheet's metadata row cascades to its structured rows. All data columns
are ``text`` (nullable), named ``col_*`` so every one is an unquoted-safe
identifier. All generated identifiers are double-quoted in DDL as internal
hygiene.
"""

import warnings

from ingpipe_lib.logconfig import get_logger
from ingpipe_lib.sql_comments import COMMENT_TEXT_PARAM, build_comment_statements
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SAWarning

from ingpipe_excel_ingestion._utils import (
    deduplicate_columns,
    normalize_column_name,
    validate_sql_identifier,
)
from ingpipe_excel_ingestion.excel_parser import ROW_NUMBER_KEY

logger = get_logger(__name__)

# The identity columns present on every structured table. Data columns are
# everything else (the col_* columns); computing the data set as "all minus
# identity" surfaces any stray column as drift instead of hiding it.
IDENTITY_COLUMNS = ("collection_path", "sort_order")


def _quote(identifier: str) -> str:
    """Double-quote an identifier for safe embedding in generated SQL.

    The identifier must already have passed ``validate_sql_identifier`` (so it
    contains no double quotes); quoting is internal hygiene.

    Args:
        identifier: A validated SQL identifier.

    Returns:
        The identifier wrapped in double quotes.
    """
    return f'"{identifier}"'


def build_column_mapping(column_names: list[str]) -> dict[str, str]:
    """Map original Excel headers to deduplicated ``col_*`` identifiers.

    The mapping is keyed on the original header, so it REQUIRES the raw
    header strings to be distinct: two byte-identical raw headers would
    collapse to one dict entry, silently dropping a column while the survivor
    took the last duplicate's values. Duplicates therefore raise. In the
    pipeline this is unreachable (``excel_parser._validate_headers`` rejects
    a sheet whose headers are identical after cleaning before this runs); the
    raise makes the helper honest for direct callers. Distinct raw headers
    that collide only after normalization (e.g. ``Code``/``code``) are
    resolved with ``_2``-style suffixes by ``deduplicate_columns``.

    Args:
        column_names: Original header strings, in sheet order.

    Returns:
        An ordered dict from original header to its ``col_*`` name. Order
        follows ``column_names``.

    Raises:
        ValueError: If two raw headers are byte-identical (the mapping cannot
            represent them without losing a column).
    """
    seen: set[str] = set()
    duplicate_set: set[str] = set()
    for header in column_names:
        if header in seen:
            duplicate_set.add(header)
        seen.add(header)
    duplicates = sorted(duplicate_set)
    if duplicates:
        raise ValueError(
            f"Duplicate raw header(s) {duplicates}: a header-keyed column "
            "mapping would silently drop a column"
        )
    normalized = [normalize_column_name(h, i) for i, h in enumerate(column_names)]
    deduped = deduplicate_columns(normalized)
    return dict(zip(column_names, deduped))


def ensure_table(
    conn: Connection,
    db_schema: str,
    table_name: str,
    sheet_table: str,
    col_names: list[str],
    table_comment: str | None = None,
) -> None:
    """Create the structured table with identity + data columns if it is absent.

    Idempotent (``create table if not exists``). The table has the identity
    columns ``collection_path ltree`` (a foreign key onto
    ``{db_schema}.{sheet_table}(collection_path)`` with ``on delete cascade``)
    and ``sort_order integer`` (``check (sort_order >= 1)``), a composite PK
    ``(collection_path, sort_order)``, and one all-text ``col_*`` data column per
    name in ``col_names``. All identifiers are validated and double-quoted.

    When ``table_comment`` is set it is applied with COMMENT ON TABLE after the
    create — unconditionally, not only on first creation, so a re-run refreshes
    the description text (COMMENT ON overwrites). The text is data, bound as a
    parameter so the driver quotes it safely.

    Args:
        conn: An open SQLAlchemy connection (inside a transaction).
        db_schema: Target schema (validated).
        table_name: Target table name (validated).
        sheet_table: Parent ``sheet`` table name, for the FK (validated).
        col_names: Deduplicated ``col_*`` data column names.
        table_comment: Optional COMMENT ON TABLE text. None leaves any existing
            comment alone.
    """
    validate_sql_identifier(db_schema, "db_schema")
    validate_sql_identifier(table_name, "table_name")
    validate_sql_identifier(sheet_table, "sheet_table")
    for col in col_names:
        validate_sql_identifier(col, "data_column")

    col_defs = [
        f"    collection_path ltree not null references "
        f"{_quote(db_schema)}.{_quote(sheet_table)} (collection_path) "
        "on delete cascade",
        "    sort_order integer not null check (sort_order >= 1)",
    ]
    col_defs.extend(f"    {_quote(col)} text" for col in col_names)
    col_defs.append("    primary key (collection_path, sort_order)")

    col_block = ",\n".join(col_defs)
    ddl = (
        f"create table if not exists "
        f"{_quote(db_schema)}.{_quote(table_name)} (\n{col_block}\n)"
    )
    logger.debug(f"Ensuring table {db_schema}.{table_name}:\n{ddl}")
    conn.execute(text(ddl))
    if table_comment is not None:
        # Applied on every call (not only first creation) so a re-run
        # refreshes the text. Built by the shared ingpipe_lib helper (see it
        # for the psycopg2 client-side binding the :comment_text parameter
        # depends on) so every leg emits identical statements.
        for statement, comment_text_value in build_comment_statements(
            db_schema, table_comments={table_name: table_comment}
        ):
            conn.execute(text(statement), {COMMENT_TEXT_PARAM: comment_text_value})
    logger.debug(
        f"Ensured structured table {db_schema}.{table_name} "
        f"with {len(col_names)} data columns"
        + (" (comment applied)" if table_comment is not None else "")
    )


def reconcile_columns(
    conn: Connection,
    db_schema: str,
    table_name: str,
    col_names: list[str],
    min_column_overlap: float,
) -> None:
    """Reconcile an existing table's data columns with an incoming sheet's.

    Reflects the existing table's data columns (all columns minus the identity
    columns) and compares them, as sets, with ``col_names``:

      - ``r_in = |intersection| / |incoming|``
      - ``r_ex = |intersection| / |existing|``

    If BOTH ratios are below ``min_column_overlap`` the sheet almost certainly
    targets the wrong table, so a ValueError is raised. Otherwise every incoming
    column not yet present is added via ``ALTER TABLE ... ADD COLUMN`` and the
    added columns (and the columns the sheet is missing) are WARN-logged so
    schema drift is visible. Columns are never dropped.

    Args:
        conn: An open SQLAlchemy connection (inside a transaction).
        db_schema: Target schema (validated).
        table_name: Target table name (validated).
        col_names: Deduplicated ``col_*`` data column names for this sheet.
        min_column_overlap: Overlap threshold in [0, 1].

    Raises:
        ValueError: If both overlap ratios are below ``min_column_overlap``.
    """
    validate_sql_identifier(db_schema, "db_schema")
    validate_sql_identifier(table_name, "table_name")
    for col in col_names:
        validate_sql_identifier(col, "data_column")

    # Reflect existing columns by NAME only. The collection_path ltree column has
    # no registered SQLAlchemy type, so reflection maps it to NullType and emits a
    # SAWarning. That is harmless here (we never use the reflected types, only the
    # names), so suppress the expected warning to keep logs/test output clean.
    inspector = inspect(conn)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SAWarning)
        reflected = {
            c["name"] for c in inspector.get_columns(table_name, schema=db_schema)
        }
    existing = reflected - set(IDENTITY_COLUMNS)
    incoming = set(col_names)
    intersection = existing & incoming

    # Guard the overlap ratios against empty denominators. An empty incoming or
    # existing data-column set is degenerate; treat its ratio as 0 so the guard
    # fires (rather than dividing by zero) when there is truly nothing in common.
    r_in = len(intersection) / len(incoming) if incoming else 0.0
    r_ex = len(intersection) / len(existing) if existing else 0.0

    if r_in < min_column_overlap and r_ex < min_column_overlap:
        raise ValueError(
            f"Incompatible columns for {db_schema}.{table_name}: incoming sheet "
            f"shares only {len(intersection)} of {len(incoming)} incoming and "
            f"{len(existing)} existing data columns "
            f"(r_in={r_in:.2f}, r_ex={r_ex:.2f} both < {min_column_overlap}). "
            "Likely a wrong-table-name clash."
        )

    to_add = [c for c in col_names if c not in existing]
    missing = sorted(existing - incoming)
    for col in to_add:
        conn.execute(
            text(
                f"alter table {_quote(db_schema)}.{_quote(table_name)} "
                f"add column {_quote(col)} text"
            )
        )
    if to_add:
        logger.warning(
            f"Schema drift on {db_schema}.{table_name}: added {len(to_add)} "
            f"column(s) {to_add}"
        )
    if missing:
        logger.warning(
            f"Schema drift on {db_schema}.{table_name}: sheet is missing "
            f"{len(missing)} existing column(s) {missing} (inserted as NULL)"
        )


def _source_present(
    conn: Connection,
    db_schema: str,
    table_name: str,
    collection_path: str,
) -> bool:
    """Return whether any rows for ``collection_path`` already exist.

    Args:
        conn: An open SQLAlchemy connection.
        db_schema: Schema name (validated by the caller).
        table_name: Table name (validated by the caller).
        collection_path: The sheet's collection_path.

    Returns:
        True if at least one row for this collection_path is present, else False.
    """
    result = conn.execute(
        text(
            f"select 1 from {_quote(db_schema)}.{_quote(table_name)} "
            "where collection_path = :cp limit 1"
        ),
        {"cp": collection_path},
    )
    return result.fetchone() is not None


def write_rows(
    engine: Engine,
    db_schema: str,
    table_name: str,
    sheet_table: str,
    collection_path: str,
    column_names: list[str],
    rows: list[dict[str, str | int | None]],
    *,
    overwrite: bool,
    min_column_overlap: float,
    table_comment: str | None = None,
) -> int:
    """Write a sheet's rows to its structured table in one transaction.

    Within a single transaction: ensure the table exists; if ``overwrite``,
    delete this collection_path's existing rows; else if it is already present,
    skip (return ``-1`` as a skipped marker). Reconcile columns (additive
    evolution + compatibility guard) against the now-existing table, then insert
    the rows. Each insert binds the identity columns (``collection_path``,
    ``sort_order``) plus the mapped ``col_*`` values; columns absent from this
    sheet are inserted as NULL. The parent ``sheet`` row for this collection_path
    must already exist (the FK requires it); the orchestrator writes it on the
    embedding leg first. All data values are bound as parameters; identifiers are
    validated and quoted.

    Args:
        engine: SQLAlchemy engine for the target database.
        db_schema: Target schema (validated).
        table_name: Target table name (validated).
        sheet_table: Parent ``sheet`` table name, for the FK (validated).
        collection_path: The sheet's ltree identity.
        column_names: Original Excel headers, in sheet order.
        rows: Parsed row dicts (header-keyed, plus ``ROW_NUMBER_KEY``).
        overwrite: If True, replace this source's rows; else skip if present.
        min_column_overlap: Overlap threshold for the compatibility guard.
        table_comment: Optional COMMENT ON TABLE description, applied by
            ``ensure_table`` on every write (refreshed, not create-only).

    Returns:
        The number of rows inserted, or ``-1`` if the source was skipped
        (already present and ``overwrite`` is False).

    Raises:
        ValueError: If an identifier is invalid, the compatibility guard
            fails, or the sheet's raw headers contain byte-identical
            duplicates (build_column_mapping refuses to drop a column).
    """
    validate_sql_identifier(db_schema, "db_schema")
    validate_sql_identifier(table_name, "table_name")
    validate_sql_identifier(sheet_table, "sheet_table")

    column_map = build_column_mapping(column_names)
    col_names = list(column_map.values())

    with engine.begin() as conn:
        ensure_table(
            conn, db_schema, table_name, sheet_table, col_names, table_comment
        )

        if overwrite:
            # Defensive: when the embedding leg overwrote (deleted) this sheet's
            # `sheet` row, the FK cascade already removed these rows. Re-deleting
            # is a harmless no-op that also covers a structured-only re-run.
            conn.execute(
                text(
                    f"delete from {_quote(db_schema)}.{_quote(table_name)} "
                    "where collection_path = :cp"
                ),
                {"cp": collection_path},
            )
        elif _source_present(conn, db_schema, table_name, collection_path):
            logger.info(
                f"Source {collection_path!r} already present in "
                f"{db_schema}.{table_name} and overwrite=false; skipping"
            )
            return -1

        # Reconcile after ensure_table so the table is guaranteed to exist; on a
        # freshly created table the incoming columns all match and nothing is added.
        reconcile_columns(
            conn, db_schema, table_name, col_names, min_column_overlap
        )

        if not rows:
            logger.debug(f"No rows to insert into {db_schema}.{table_name}")
            return 0

        insert_cols = ["collection_path", "sort_order", *col_names]
        col_sql = ", ".join(
            ["collection_path", "sort_order", *[_quote(c) for c in col_names]]
        )
        param_sql = ", ".join(f":{c}" for c in insert_cols)
        insert_sql = text(
            f"insert into {_quote(db_schema)}.{_quote(table_name)} "
            f"({col_sql}) values ({param_sql})"
        )

        all_params: list[dict[str, str | int | None]] = []
        for row in rows:
            params: dict[str, str | int | None] = {
                "collection_path": collection_path,
                "sort_order": row[ROW_NUMBER_KEY],
            }
            for header, col in column_map.items():
                params[col] = row.get(header)
            all_params.append(params)

        conn.execute(insert_sql, all_params)

    logger.info(
        f"Structured: {collection_path} -> {db_schema}.{table_name}: "
        f"{len(rows)} rows"
    )
    return len(rows)
