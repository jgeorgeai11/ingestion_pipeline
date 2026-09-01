"""Output validation for the load step's document / document_content tables.

Validates the data at rest in PostgreSQL after ``ingest.py``'s load step. This
is the SQL-based counterpart to ``data_val_cleaned_json`` (which validates the
on-disk cleaned JSON): it queries the loaded ``document`` and
``document_content`` tables and asserts the cross-table invariants that only
hold once the data is in the database.

The two tables are validated together because the key checks (FK integrity and
the count match) span both.

This validator runs after a load completes and assumes no concurrent writes,
so cross-statement snapshot differences (each statement gets its own snapshot
under the default READ COMMITTED isolation) are not a concern.

Checks (all SQL):
  - The schema is not empty (at least one document loaded).
  - Every document the config expects is present by its ``collection_path``.
  - Every ``document.collection_path`` is a non-null, valid ``ltree``.
  - Every ``document.source_binary_hash`` is non-null and a valid unsigned
    64-bit integer (in ``[0, 2^64)``) — the source provenance every loaded
    document now carries.
  - ``document.n_parsed_sections`` equals the ``count(*)`` of that document's
    ``document_content`` rows.
  - Every document has at least one ``document_content`` row (mirroring
    ``CleanedDocument``'s "has at least one section" invariant).
  - No orphan ``document_content`` rows (referential integrity).
  - ``sort_order`` per document is 1-based and contiguous (1..N).
  - ``word_count`` is non-null and ``>= 0``.
  - ``page_start <= page_end`` wherever both are non-null.

Usage:
    uv run data-val-loaded-documents \
        --config instances/<instance>/config/ingpipe_file_ingestion/<name>.toml \
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
from ingpipe_lib.db import get_engine
from ingpipe_lib.logconfig import get_logger
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

# Reuse the identifier validator so schema/table names are validated exactly
# as the loader does, plus the loader's own config validator, db-target
# resolver, and document filter so the expected document keys are derived
# exactly as the loader derives them — a config the ingester accepts can
# never be one this validator rejects, and vice versa.
from ingpipe_file_ingestion._utils import validate_sql_identifier
from ingpipe_file_ingestion.ingest import (
    DOCUMENT_CONTENT_TABLE,
    DOCUMENT_TABLE,
    filter_valid_documents,
    resolve_db_target,
    validate_config,
)

logger = get_logger(__name__)


def validate_loaded_documents(
    engine: Engine,
    db_schema: str,
    document_table: str,
    content_table: str,
    expected_collection_paths: list[str],
) -> list[str]:
    """Run the SQL invariant checks over the loaded document tables.

    Each check is a query that returns offending rows; any returned row becomes
    one ``FAIL`` message. Identifiers are validated before interpolation, so the
    schema/table names cannot inject SQL.

    The checks confirm the schema is non-empty, that every document the config
    expects is present (by its validated ``collection_path``), and the
    cross-table invariants over the loaded rows -- including that every document
    has at least one content row.

    The checks run as multiple statements under the default READ COMMITTED
    isolation; the validator assumes no concurrent writes (it runs after a load
    completes), so cross-statement snapshot differences are not a concern.

    Args:
        engine: SQLAlchemy Engine connected to the target database.
        db_schema: PostgreSQL schema holding the tables.
        document_table: Name of the document table.
        content_table: Name of the document_content table.
        expected_collection_paths: Validated collection_paths the config's
            documents should have produced; each must exist in the document
            table.

    Returns:
        A list of failure messages; empty when every check passes.

    Raises:
        ValueError: If any identifier is not a safe SQL identifier.
        SQLAlchemyError: If a check query fails (e.g. a missing table).
    """
    validate_sql_identifier(db_schema, "db_schema")
    validate_sql_identifier(document_table, "document_table")
    validate_sql_identifier(content_table, "content_table")

    doc = f"{db_schema}.{document_table}"
    content = f"{db_schema}.{content_table}"
    failures: list[str] = []

    with engine.connect() as conn:
        # 0. There must be at least one document; an empty schema is a failure
        # (the load did not populate the tables this config expects).
        n_docs = conn.execute(text(f"select count(*) from {doc}")).scalar_one()
        if n_docs == 0:
            return [f"FAIL: {doc} is empty (no documents loaded)"]
        logger.info(f"Found {n_docs} document(s) in {doc}")

        # 0a. Every document the config expects must be present. Validating
        # against the config's own documents (not just whatever rows exist)
        # confirms this config's load landed, mirroring data_val_cleaned_json's
        # per-expected-file checking.
        present = {
            row[0]
            for row in conn.execute(
                text(
                    f"select collection_path::text from {doc} "
                    "where collection_path::text = any(:cps)"
                ),
                {"cps": expected_collection_paths},
            ).fetchall()
        }
        for cp in expected_collection_paths:
            if cp not in present:
                failures.append(f"FAIL: expected document not loaded: {cp}")
        logger.info(
            f"Expected documents present: {len(present)}/{len(expected_collection_paths)}"
        )

        # 1. collection_path is a non-null, valid ltree. The column is typed
        # ltree, so any persisted value already parses; a null PK is impossible,
        # but check explicitly so the contract is asserted, not assumed.
        rows = conn.execute(
            text(f"select count(*) from {doc} where collection_path is null")
        ).scalar_one()
        if rows:
            failures.append(f"FAIL: {doc}: {rows} row(s) with null collection_path")

        # 1a. source_binary_hash is non-null and a valid unsigned 64-bit integer
        # (in [0, 2^64)). The column is numeric(20,0) and NOT NULL, so a null is
        # impossible, but assert the contract explicitly; the range check guards
        # against any out-of-band value (a negative or an overflow past uint64).
        rows = conn.execute(
            text(
                f"""
                select count(*) from {doc}
                where source_binary_hash is null
                    or source_binary_hash < 0
                    or source_binary_hash >= :ceiling
                """
            ),
            {"ceiling": UINT64_CEILING},
        ).scalar_one()
        if rows:
            failures.append(
                f"FAIL: {doc}: {rows} row(s) with null/out-of-range "
                "source_binary_hash (expected a uint64 in [0, 2^64))"
            )

        # 2. n_parsed_sections equals the content row count per document.
        rows = conn.execute(
            text(
                f"""
                select doc_row.collection_path::text as cp,
                       doc_row.n_parsed_sections as expected,
                       count(content_row.sort_order) as actual
                from {doc} doc_row
                left join {content} content_row
                    on content_row.collection_path = doc_row.collection_path
                group by doc_row.collection_path, doc_row.n_parsed_sections
                having doc_row.n_parsed_sections <> count(content_row.sort_order)
                """
            )
        ).fetchall()
        for cp, expected, actual in rows:
            failures.append(
                f"FAIL: {cp}: n_parsed_sections ({expected}) != content rows ({actual})"
            )

        # 2a. Every document has at least one content row. Check 2 alone passes a
        # document with n_parsed_sections = 0 and 0 content rows (0 == 0), and
        # the contiguity check produces no group for it, so assert non-emptiness
        # explicitly -- mirroring CleanedDocument's "has at least one section".
        # count(content_row.sort_order) is exact here because sort_order is NOT
        # NULL, so the left-join count is 0 (not 1) for a content-less document.
        rows = conn.execute(
            text(
                f"""
                select doc_row.collection_path::text as cp
                from {doc} doc_row
                left join {content} content_row
                    on content_row.collection_path = doc_row.collection_path
                group by doc_row.collection_path
                having count(content_row.sort_order) = 0
                """
            )
        ).fetchall()
        for (cp,) in rows:
            failures.append(f"FAIL: document {cp} has zero content rows")

        # 3. No orphan content rows (FK integrity). The FK constraint enforces
        # this, but validate independently so the data is checked, not the DDL.
        rows = conn.execute(
            text(
                f"""
                select content_row.collection_path::text as cp, count(*) as n
                from {content} content_row
                left join {doc} doc_row
                    on doc_row.collection_path = content_row.collection_path
                where doc_row.collection_path is null
                group by content_row.collection_path
                """
            )
        ).fetchall()
        for cp, n in rows:
            failures.append(f"FAIL: {cp}: {n} orphan content row(s) (no parent document)")

        # 4. sort_order is 1-based and contiguous (1..N) per document. Compare
        # the actual set against the expected dense sequence via min/max/count.
        # min(sort_order)=1 and max(sort_order)=count(*) is a correct contiguity
        # test *because* the composite PK (collection_path, sort_order) guarantees
        # sort_order is unique within a document, so count(*) equals the number of
        # distinct values; without that uniqueness a sequence like {1, 1, 2} could
        # slip through.
        rows = conn.execute(
            text(
                f"""
                select collection_path::text as cp,
                       min(sort_order) as lo,
                       max(sort_order) as hi,
                       count(*) as n,
                       count(distinct sort_order) as n_distinct
                from {content}
                group by collection_path
                having min(sort_order) <> 1
                    or max(sort_order) <> count(*)
                """
            )
        ).fetchall()
        for cp, lo, hi, n, n_distinct in rows:
            failures.append(
                f"FAIL: {cp}: sort_order not 1-based contiguous "
                f"(min={lo}, max={hi}, count={n}, distinct={n_distinct})"
            )

        # 5. word_count is non-null and >= 0.
        rows = conn.execute(
            text(
                f"""
                select collection_path::text as cp, count(*) as n
                from {content}
                where word_count is null or word_count < 0
                group by collection_path
                """
            )
        ).fetchall()
        for cp, n in rows:
            failures.append(f"FAIL: {cp}: {n} row(s) with null/negative word_count")

        # 6. page_start <= page_end wherever both are non-null.
        rows = conn.execute(
            text(
                f"""
                select collection_path::text as cp, count(*) as n
                from {content}
                where page_start is not null
                    and page_end is not null
                    and page_start > page_end
                group by collection_path
                """
            )
        ).fetchall()
        for cp, n in rows:
            failures.append(f"FAIL: {cp}: {n} row(s) with page_start > page_end")

    return failures


def main() -> None:
    """Entry point for loaded-document output validation."""
    # 1. Parse arguments (the canonical --config/--env-file pair).
    parser = build_parser(
        "Validate the load step's document/document_content tables"
    )
    args = parser.parse_args()

    # 2. Setup logging (after argparse so --help doesn't create log files):
    # INFO level, named from the config stem, anchored to the instance root.
    config_path = Path(args.config)
    setup_entry_logging("ingpipe_file_ingestion/data_validation", config_path)

    with run_scope():
        logger.info("Starting output validation for loaded document tables")

        # 3. Resolve credentials and load the TOML config (exits 1 on a
        # missing env file, missing config, or malformed TOML).
        config = load_config(config_path, args.env_file)

        # 4. Gate the config through the INGESTER'S OWN validator, so a config
        # the ingester accepts can never be one this validator rejects.
        try:
            validate_config(config)
        except ValueError as e:
            logger.error(f"Invalid config value: {e}")
            sys.exit(1)

        # 5. Resolve the database/schema/table names exactly as the loader
        # does (top-level db target with the deprecated [load] fallback), and
        # derive the expected documents with the loader's own filter, which
        # warns-and-skips a document whose collection_path is
        # missing/blank/invalid and raises on the duplicate-file/
        # duplicate-path config errors the loader aborts on.
        db_name, db_schema = resolve_db_target(config)
        load_cfg = config.get("load", {})
        document_table = load_cfg.get("document_table", DOCUMENT_TABLE)
        content_table = load_cfg.get("content_table", DOCUMENT_CONTENT_TABLE)
        documents = config["module"]["documents"]
        try:
            _, collection_paths = filter_valid_documents(documents)
        except ValueError as e:
            logger.error(f"Invalid config value: {e}")
            sys.exit(1)
        expected_collection_paths = list(collection_paths.values())

        if not expected_collection_paths:
            logger.error("Config lists no documents with a valid collection_path")
            sys.exit(1)

        logger.info(
            f"Validating {db_name}.{db_schema}.{document_table} / {content_table} "
            f"({len(expected_collection_paths)} expected document(s))"
        )

        # 6. Connect and run the SQL checks. Dispose the engine (and its
        # pool) in a finally so no pooled connections are left open if the
        # process is reused.
        try:
            engine = get_engine(db_name)
        except ValueError as e:
            logger.error(f"Invalid configuration: {e}")
            sys.exit(1)

        try:
            all_failures = validate_loaded_documents(
                engine, db_schema, document_table, content_table,
                expected_collection_paths,
            )
        except ValueError as e:
            logger.error(f"Invalid configuration: {e}")
            sys.exit(1)
        except SQLAlchemyError as e:
            logger.error(f"Database error during validation: {e}")
            sys.exit(1)
        finally:
            engine.dispose()

        finish_run(
            all_failures,
            success_message=(
                "OUTPUT VALIDATION PASSED: all document/content invariants hold"
            ),
            failure_prefix="OUTPUT VALIDATION FAILED",
        )


if __name__ == "__main__":
    main()
