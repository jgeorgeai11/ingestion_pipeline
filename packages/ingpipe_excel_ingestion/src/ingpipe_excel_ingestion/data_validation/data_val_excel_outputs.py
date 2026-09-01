"""Output validation for the generic Excel ingestion module (post-ingest, DB).

Validates BOTH legs of the hybrid storage model:

  - Embedding leg (``sheet`` / ``sheet_content``): every ``sheet`` metadata row
    has >=1 ``sheet_content`` row (FK/cascade intact), no orphan content rows,
    every ``row_text`` is non-empty, every ``word_count`` >= 0, ``sort_order`` is
    contiguous (1..N) per ``collection_path``, every ``source_binary_hash`` is in
    ``[0, 2^64)``, ``n_rows`` equals the actual content-row count, and every
    configured sheet's ``collection_path`` has a ``sheet`` row.
  - Structured leg (each table named by config): the table exists, has rows, no
    identity column (``collection_path``/``sort_order``) is null, every configured
    source ``collection_path`` is present, and no structured row references a
    missing ``sheet`` row (FK to ``sheet`` satisfied).

Failures accumulate; the script exits 1 if any check fails.

Usage:
    uv run data-val-excel-outputs \
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
from ingpipe_lib.constants import UINT64_CEILING
from ingpipe_lib.logconfig import get_logger
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from ingpipe_excel_ingestion._utils import get_engine, make_collection_path, validate_sql_identifier

# The ingester's own config validator and consolidated-table defaults: a
# config it accepts can never be one this validator rejects, and the default
# table names cannot drift between producer and validator.
from ingpipe_excel_ingestion.ingest_excel import CONTENT_TABLE, SHEET_TABLE, validate_config

logger = get_logger(__name__)


def validate_content_leg(
    engine: Engine,
    db_schema: str,
    sheet_table: str,
    content_table: str,
    expected_paths: set[str],
) -> list[str]:
    """Validate the consolidated (sheet metadata / content) embedding leg.

    Args:
        engine: SQLAlchemy engine.
        db_schema: Target schema (validated by the caller).
        sheet_table: Sheet metadata table name (validated by the caller).
        content_table: Per-row content table name (validated by the caller).
        expected_paths: Every configured sheet's collection_path; each must have
            a sheet-metadata row (the embedding leg is always written).

    Returns:
        A list of failure messages (empty if the leg is sound).
    """
    failures: list[str] = []
    with engine.connect() as conn:
        # Every sheet metadata row must have at least one content row (cascade /
        # FK intact). The ingester only writes sheets with data, so a 0-content
        # metadata row is a failure (and would violate the n_rows >= 1 CHECK).
        orphan_meta = conn.execute(
            text(
                "select sht.collection_path "
                f"from {db_schema}.{sheet_table} sht "
                f"left join {db_schema}.{content_table} cnt "
                "  on cnt.collection_path = sht.collection_path "
                "where cnt.collection_path is null"
            )
        ).fetchall()
        for (collection_path,) in orphan_meta:
            failures.append(
                f"FAIL: sheet row {collection_path} has no sheet_content rows"
            )

        # Content rows must not reference a missing sheet row (FK integrity).
        orphan_content = conn.execute(
            text(
                f"select count(*) from {db_schema}.{content_table} cnt "
                f"where not exists (select 1 from {db_schema}.{sheet_table} sht "
                "  where sht.collection_path = cnt.collection_path)"
            )
        ).scalar()
        if orphan_content:
            failures.append(
                f"FAIL: {orphan_content} sheet_content row(s) reference a missing "
                "sheet metadata row"
            )

        # row_text non-empty.
        empty_text = conn.execute(
            text(
                f"select count(*) from {db_schema}.{content_table} "
                "where row_text is null or btrim(row_text) = ''"
            )
        ).scalar()
        if empty_text:
            failures.append(
                f"FAIL: {empty_text} sheet_content row(s) have empty row_text"
            )

        # word_count >= 0.
        bad_wc = conn.execute(
            text(
                f"select count(*) from {db_schema}.{content_table} "
                "where word_count < 0"
            )
        ).scalar()
        if bad_wc:
            failures.append(
                f"FAIL: {bad_wc} sheet_content row(s) have word_count < 0"
            )

        # source_binary_hash present and within the unsigned 64-bit range.
        bad_hash = conn.execute(
            text(
                f"select count(*) from {db_schema}.{sheet_table} "
                "where source_binary_hash is null "
                "or source_binary_hash < 0 "
                "or source_binary_hash >= :ceiling"
            ),
            {"ceiling": UINT64_CEILING},
        ).scalar()
        if bad_hash:
            failures.append(
                f"FAIL: {bad_hash} sheet row(s) have an out-of-range "
                "source_binary_hash"
            )

        # n_rows on the sheet row must equal the actual content-row count.
        bad_n_rows = conn.execute(
            text(
                "select sht.collection_path, sht.n_rows, count(cnt.sort_order) as actual "
                f"from {db_schema}.{sheet_table} sht "
                f"left join {db_schema}.{content_table} cnt "
                "  on cnt.collection_path = sht.collection_path "
                "group by sht.collection_path, sht.n_rows "
                "having sht.n_rows <> count(cnt.sort_order)"
            )
        ).fetchall()
        for collection_path, n_rows, actual in bad_n_rows:
            failures.append(
                f"FAIL: {collection_path} n_rows={n_rows} but has {actual} "
                "content rows"
            )

        # sort_order contiguous 1..N per collection_path: the count must equal
        # the max and the min must be 1.
        bad_order = conn.execute(
            text(
                "select collection_path, count(*) as n, "
                "  min(sort_order) as mn, max(sort_order) as mx "
                f"from {db_schema}.{content_table} "
                "group by collection_path "
                "having min(sort_order) <> 1 or max(sort_order) <> count(*)"
            )
        ).fetchall()
        for collection_path, n, mn, mx in bad_order:
            failures.append(
                f"FAIL: {collection_path} sort_order not contiguous: "
                f"n={n}, min={mn}, max={mx}"
            )

        # Every always-written sheet must have a sheet row; a silently-skipped or
        # failed universal-leg sheet would be absent here (the structured leg has
        # the analogous per-source presence check).
        present = {
            row[0]
            for row in conn.execute(
                text(f"select collection_path::text from {db_schema}.{sheet_table}")
            ).fetchall()
        }
        for collection_path in sorted(expected_paths - present):
            failures.append(
                f"FAIL: configured sheet {collection_path} has no sheet row "
                "(embedding leg)"
            )

    if not failures:
        logger.info(f"PASS: content leg sound in {db_schema}")
    return failures


def validate_structured_table(
    engine: Engine,
    db_schema: str,
    sheet_table: str,
    table_name: str,
    collection_paths: set[str],
) -> list[str]:
    """Validate one structured table and the presence of its configured sources.

    Args:
        engine: SQLAlchemy engine.
        db_schema: Target schema (validated by the caller).
        sheet_table: Sheet metadata table the structured table's FK references
            (validated by the caller).
        table_name: Structured table name (validated).
        collection_paths: The collection_paths that should be present.

    Returns:
        A list of failure messages (empty if the table is sound).
    """
    validate_sql_identifier(table_name, "table_name")
    failures: list[str] = []

    with engine.connect() as conn:
        exists = conn.execute(
            text(
                "select 1 from information_schema.tables "
                "where table_schema = :schema and table_name = :tbl"
            ),
            {"schema": db_schema, "tbl": table_name},
        ).fetchone()
        if exists is None:
            return [f"FAIL: structured table {db_schema}.{table_name} does not exist"]

        row_count = conn.execute(
            text(f"select count(*) from {db_schema}.{table_name}")
        ).scalar()
        if not row_count:
            failures.append(f"FAIL: {db_schema}.{table_name} has 0 rows")
            return failures

        null_identity = conn.execute(
            text(
                f"select count(*) from {db_schema}.{table_name} "
                "where collection_path is null or sort_order is null"
            )
        ).scalar()
        if null_identity:
            failures.append(
                f"FAIL: {db_schema}.{table_name} has {null_identity} row(s) with "
                "a null identity column"
            )

        # FK to sheet: no structured row may reference a missing sheet row.
        orphan = conn.execute(
            text(
                f"select count(*) from {db_schema}.{table_name} strct "
                f"where not exists (select 1 from {db_schema}.{sheet_table} sht "
                "  where sht.collection_path = strct.collection_path)"
            )
        ).scalar()
        if orphan:
            failures.append(
                f"FAIL: {db_schema}.{table_name} has {orphan} row(s) with no "
                "matching sheet row (orphan FK)"
            )

        for collection_path in sorted(collection_paths):
            present = conn.execute(
                text(
                    f"select 1 from {db_schema}.{table_name} "
                    "where collection_path = :cp limit 1"
                ),
                {"cp": collection_path},
            ).fetchone()
            if present is None:
                failures.append(
                    f"FAIL: {db_schema}.{table_name} missing source "
                    f"{collection_path}"
                )

    if not failures:
        logger.info(f"PASS: {db_schema}.{table_name} sound ({row_count:,} rows)")
    return failures


def main() -> None:
    """Entry point for output validation."""
    parser = build_parser("Validate ingested Excel output (both hybrid legs)")
    args = parser.parse_args()

    # INFO level, named from the config stem, anchored to the instance root.
    config_path = Path(args.config)
    setup_entry_logging("ingpipe_excel_ingestion/data_validation", config_path)

    with run_scope():
        logger.info("Starting output validation for Excel ingestion")

        # Resolve credentials and load the TOML config (exits 1 on a missing
        # env file, missing config, or malformed TOML).
        config = load_config(config_path, args.env_file)

        # Gate the config through the INGESTER'S OWN validator, so a config
        # the ingester accepts can never be one this validator rejects.
        try:
            validate_config(config)
        except ValueError as e:
            logger.error(f"Config validation failed: {e}")
            sys.exit(1)

        db_name = config["db_name"]
        db_schema = config["db_schema"]
        files = config["files"]
        # Honor the same configurable consolidated-table names the ingester
        # uses, defaulting to the standard pair, so a custom-named run
        # validates the tables it actually wrote.
        sheet_table = config.get("sheet_table", SHEET_TABLE)
        content_table = config.get("content_table", CONTENT_TABLE)

        # Collect every configured sheet's collection_path (for the
        # embedding-leg presence check) and the per-structured-table sources,
        # deriving each path the same way the ingester does.
        try:
            table_sources: dict[str, set[str]] = {}
            all_paths: set[str] = set()
            for filename, file_entry in files.items():
                for sheet_entry in file_entry["sheets"]:
                    # The prefix MUST be passed here too: this validator
                    # independently re-derives every path to check what the
                    # ingester stored, so omitting it would compute prefix-less
                    # paths, find none of them, and report every sheet missing
                    # -- a false failure indistinguishable from a real one.
                    collection_path = make_collection_path(
                        filename,
                        sheet_entry["sheet"],
                        sheet_entry.get("collection_path"),
                        config.get("collection_path_prefix"),
                    )
                    all_paths.add(collection_path)
                    table = sheet_entry.get("table")
                    if table is not None:
                        table_sources.setdefault(table, set()).add(collection_path)
        except ValueError as e:
            logger.error(f"Config resolution failed: {e}")
            sys.exit(1)

        try:
            validate_sql_identifier(db_schema, "db_schema")
            validate_sql_identifier(sheet_table, "sheet_table")
            validate_sql_identifier(content_table, "content_table")
            engine = get_engine(db_name)
        except ValueError as e:
            logger.error(f"Invalid configuration: {e}")
            sys.exit(1)

        # Dispose the engine (and its pool) in a finally so no pooled
        # connections are left open if the process is reused.
        try:
            all_failures: list[str] = []
            all_failures.extend(
                validate_content_leg(
                    engine, db_schema, sheet_table, content_table, all_paths
                )
            )
            for table_name, sources in sorted(table_sources.items()):
                logger.info(f"--- Validating {db_schema}.{table_name} ---")
                all_failures.extend(
                    validate_structured_table(
                        engine, db_schema, sheet_table, table_name, sources
                    )
                )
        except (SQLAlchemyError, ValueError) as e:
            logger.error(f"Validation failed with error: {e}")
            sys.exit(1)
        finally:
            engine.dispose()

        finish_run(
            all_failures,
            success_message="OUTPUT VALIDATION PASSED: All checks passed",
            failure_prefix="OUTPUT VALIDATION FAILED",
        )


if __name__ == "__main__":
    main()
