"""Output validation for generated embedding tables.

Validates the data at rest in PostgreSQL after ``generate_embeddings.py`` runs.
Given the same TOML config used to generate the embeddings, it resolves the
database, schema, source table, and embedding table, then asserts the
invariants that prove the embeddings are complete, untruncated, and
hybrid-ready.

Checks (per configured source table):
  - Every source row (accounting for ``source_filter``) has at least one
    embedding row — no unembedded rows.
  - No stored ``chunk_text`` exceeds ``max_tokens`` when tokenized with the
    model's own tokenizer, using the SAME counting convention as the chunker
    (this proves the truncation bug is fixed).
  - ``embedding`` is non-null with the expected model dimension.
  - No orphan embedding rows (FK integrity).
  - The ``chunk_tsv`` generated column and its GIN index exist.

Usage:
    uv run data-val-embeddings \
        --config instances/<instance>/config/ingpipe_embedding_generation/<name>.toml \
        --env-file instances/<instance>/.env
"""

import sys
from collections.abc import Callable
from pathlib import Path

from ingpipe_lib.cli import (
    build_parser,
    finish_run,
    load_config,
    run_scope,
    setup_entry_logging,
)
from ingpipe_lib.db import get_engine
from ingpipe_lib.logconfig import get_logger
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

# _utils and the generation helpers live in the ingpipe_embedding_generation package
# root (one level up). Reuse them so the validator counts tokens, builds the
# filter clause, detects primary keys, validates identifiers, and gates the
# config exactly as the generator does.
from ingpipe_embedding_generation._utils import make_token_counter, validate_sql_identifier
from ingpipe_embedding_generation.generate_embeddings import (
    ConfigurationError,
    _build_source_filter_clause,
    _detect_primary_keys,
    _get_embedding_model,
    validate_config,
)

logger = get_logger(__name__)


def validate_embeddings(
    engine: Engine,
    db_schema: str,
    source_table: str,
    embedding_table: str,
    pk_columns: list[str],
    max_tokens: int,
    max_seq_length: int,
    expected_dimension: int,
    count_tokens: Callable[[str], int],
    source_filter: dict | None = None,
) -> list[str]:
    """Run the SQL/tokenizer invariant checks over one embedding table.

    Each check returns offending rows; any offender becomes one ``FAIL``
    message. Identifiers are validated before interpolation, so the schema and
    table names cannot inject SQL.

    Args:
        engine: SQLAlchemy Engine connected to the target database.
        db_schema: PostgreSQL schema holding the tables.
        source_table: Name of the source table.
        embedding_table: Name of the embedding table.
        pk_columns: Primary key columns shared by source and embedding tables.
        max_tokens: Token budget the chunker packed splittable chunks to. Only
            multi-word chunks are required to stay within it; a single-word chunk
            (a space-free string the chunker cannot split without breaking a word)
            may legitimately exceed it.
        max_seq_length: The model's encode-time context window. NO chunk may
            exceed this — that is the gate proving embeddings are never truncated.
        expected_dimension: Expected embedding vector dimension.
        count_tokens: Callable returning the model's token count for a string,
            using the same convention the chunker used to build the chunks.
        source_filter: Optional source_filter applied at generation time, so the
            "every source row is embedded" check is scoped to the same rows.

    Returns:
        A list of failure messages; empty when every check passes.

    Raises:
        ValueError: If any identifier is not a safe SQL identifier.
        SQLAlchemyError: If a check query fails (e.g. a missing table).
    """
    validate_sql_identifier(db_schema, "db_schema")
    validate_sql_identifier(source_table, "source_table")
    validate_sql_identifier(embedding_table, "embedding_table")
    for col in pk_columns:
        validate_sql_identifier(col, f"pk_column '{col}'")

    src = f"{db_schema}.{source_table}"
    emb = f"{db_schema}.{embedding_table}"
    failures: list[str] = []

    insp = inspect(engine)
    if not insp.has_table(embedding_table, schema=db_schema):
        return [f"FAIL: embedding table {emb} does not exist"]

    join_conditions = " and ".join(f"src.{col} = emb.{col}" for col in pk_columns)
    filter_clause, filter_params = _build_source_filter_clause(source_filter, table_alias="src")

    with engine.connect() as conn:
        # 0. There must be at least one embedding row; an empty table means the
        # generation step produced nothing for this config.
        n_emb = conn.execute(text(f"select count(*) from {emb}")).scalar_one()
        if n_emb == 0:
            return [f"FAIL: {emb} is empty (no embeddings generated)"]
        logger.info(f"Found {n_emb} embedding row(s) in {emb}")

        # 1. Every source row (scoped by source_filter) has >= 1 embedding row.
        src_filter_where = f" where {filter_clause}" if filter_clause else ""
        unembedded = conn.execute(
            text(
                f"""
                select count(*) from {src} as src
                where not exists (
                    select 1 from {emb} as emb where {join_conditions}
                ){' and ' + filter_clause if filter_clause else ''}
                """
            ),
            filter_params,
        ).scalar_one()
        n_src = conn.execute(
            text(f"select count(*) from {src} as src{src_filter_where}"),
            filter_params,
        ).scalar_one()
        if unembedded:
            failures.append(
                f"FAIL: {src}: {unembedded}/{n_src} source row(s) have no embedding"
            )
        logger.info(f"Source rows embedded: {n_src - unembedded}/{n_src}")

        # 2. embedding is non-null with the expected dimension. vector_dims()
        # reports the stored vector's dimension; a null embedding is also a fail.
        bad_embeddings = conn.execute(
            text(
                f"""
                select count(*) from {emb}
                where embedding is null or vector_dims(embedding) <> :dim
                """
            ),
            {"dim": expected_dimension},
        ).scalar_one()
        if bad_embeddings:
            failures.append(
                f"FAIL: {emb}: {bad_embeddings} row(s) with null embedding "
                f"or dimension != {expected_dimension}"
            )
        logger.info(f"Embeddings non-null at dimension {expected_dimension}: verified")

        # 3. No orphan embedding rows (FK integrity). The FK enforces this, but
        # validate the data independently rather than trusting the DDL.
        orphans = conn.execute(
            text(
                f"""
                select count(*) from {emb} as emb
                where not exists (
                    select 1 from {src} as src where {join_conditions}
                )
                """
            )
        ).scalar_one()
        if orphans:
            failures.append(f"FAIL: {emb}: {orphans} orphan embedding row(s) (no parent source)")

        # 4. chunk_tsv GENERATED column + GIN index exist (hybrid-ready storage).
        # Require is_generated = 'ALWAYS' so a manually-populated tsvector column
        # (which could silently drift from chunk_text) does not pass this check.
        tsv_col = conn.execute(
            text(
                """
                select count(*) from information_schema.columns
                where table_schema = :schema and table_name = :tbl
                    and column_name = 'chunk_tsv'
                    and is_generated = 'ALWAYS'
                """
            ),
            {"schema": db_schema, "tbl": embedding_table},
        ).scalar_one()
        if not tsv_col:
            failures.append(f"FAIL: {emb}: missing chunk_tsv generated column")

        gin_index = conn.execute(
            text(
                """
                select count(*) from pg_indexes
                where schemaname = :schema and tablename = :tbl
                    and indexdef ilike '%using gin%chunk_tsv%'
                """
            ),
            {"schema": db_schema, "tbl": embedding_table},
        ).scalar_one()
        if not gin_index:
            failures.append(f"FAIL: {emb}: missing GIN index on chunk_tsv")
        if tsv_col and gin_index:
            logger.info("Hybrid-ready: chunk_tsv column and GIN index present")

        # 5. Token checks. Tokenize each stored chunk with the model's own
        # tokenizer (the same convention the chunker used) and apply two gates:
        #   (a) Truncation gate (hard FAIL): NO chunk may exceed the model's
        #       encode-time context (max_seq_length). This is what proves
        #       embeddings are never silently truncated.
        #   (b) Budget gate: multi-word chunks must stay within max_tokens (a
        #       multi-word over-budget chunk would be a chunker regression). A
        #       single-word chunk that exceeds max_tokens is the chunker
        #       correctly refusing to split a space-free string mid-word; it is
        #       logged as a warning, not a failure (it still embeds in full
        #       because it is under max_seq_length).
        # STREAMED (yield_per -> server-side cursor) rather than fetchall():
        # the chunk set is corpus-sized, and the generator was deliberately
        # refactored to stream it — the validator must not re-materialize it.
        over_seq_length = 0
        worst_seq_tokens = 0
        multiword_over_budget = 0
        worst_budget_tokens = 0
        singleword_over_budget = 0
        worst_singleword_tokens = 0
        n_chunks = 0
        stream_conn = conn.execution_options(yield_per=1000)
        result = stream_conn.execute(
            text(f"select chunk_text, word_count from {emb}")
        )
        for chunk_text, word_count in result:
            n_chunks += 1
            n_tokens = count_tokens(chunk_text)
            if n_tokens > max_seq_length:
                over_seq_length += 1
                worst_seq_tokens = max(worst_seq_tokens, n_tokens)
            if n_tokens > max_tokens:
                if word_count > 1:
                    multiword_over_budget += 1
                    worst_budget_tokens = max(worst_budget_tokens, n_tokens)
                else:
                    singleword_over_budget += 1
                    worst_singleword_tokens = max(worst_singleword_tokens, n_tokens)

    if over_seq_length:
        failures.append(
            f"FAIL: {emb}: {over_seq_length}/{n_chunks} chunk(s) exceed the "
            f"model context max_seq_length={max_seq_length} (worst={worst_seq_tokens} "
            "tokens) — these would be truncated at embed time"
        )
    if multiword_over_budget:
        failures.append(
            f"FAIL: {emb}: {multiword_over_budget}/{n_chunks} multi-word chunk(s) "
            f"exceed max_tokens={max_tokens} (worst={worst_budget_tokens} tokens) — "
            "chunker did not honor the token budget"
        )
    if singleword_over_budget:
        # Not a failure: an unsplittable space-free word the chunker correctly
        # emitted whole. It still embeds in full (under max_seq_length).
        logger.warning(
            f"{emb}: {singleword_over_budget} single-word chunk(s) exceed "
            f"max_tokens={max_tokens} (worst={worst_singleword_tokens} tokens) — "
            "unsplittable space-free strings (e.g. markdown table separators); "
            "embed in full, future ingestion cleaning could drop them"
        )
    logger.info(
        f"Truncation gate: all {n_chunks} chunk(s) within "
        f"max_seq_length={max_seq_length}"
        if not over_seq_length
        else f"Truncation gate: {over_seq_length} chunk(s) over max_seq_length"
    )

    return failures


def main() -> None:
    """Entry point for embedding output validation."""
    # 1. Parse arguments (the canonical --config/--env-file pair).
    parser = build_parser("Validate generated embedding tables in PostgreSQL")
    args = parser.parse_args()

    # 2. Setup logging (after argparse so --help doesn't create log files):
    # INFO level, named from the config stem, anchored to the instance root.
    config_path = Path(args.config)
    setup_entry_logging("ingpipe_embedding_generation/data_validation", config_path)

    with run_scope():
        logger.info("Starting output validation for embedding tables")

        # 3. Resolve credentials and load the TOML config (exits 1 on a
        # missing env file, missing config, or malformed TOML).
        config = load_config(config_path, args.env_file)

        # 4. Gate the config through the GENERATOR'S OWN validator, so a
        # config the generator accepts can never be one this validator
        # rejects.
        try:
            validate_config(config)
        except ValueError as e:
            logger.error(f"Invalid config value: {e}")
            sys.exit(1)

        db_name = config["db_name"]
        db_schema = config["db_schema"]
        model_name = config["model_name"]
        tables = config["tables"]
        default_max_tokens = config.get("max_tokens", 500)

        # 5. Load the model once for the tokenizer (token counter) and
        # dimension.
        try:
            model = _get_embedding_model(model_name)
            expected_dimension = model.get_sentence_embedding_dimension()
            if expected_dimension is None:
                raise ValueError(
                    f"Model {model_name!r} reports no embedding dimension"
                )
            max_seq_length = model.max_seq_length
            count_tokens = make_token_counter(model)
        except (ValueError, OSError) as e:
            logger.error(f"Failed to load embedding model {model_name!r}: {e}")
            sys.exit(1)

        logger.info(
            f"Validating embeddings in {db_name}.{db_schema} "
            f"(model={model_name}, dimension={expected_dimension}, "
            f"max_seq_length={max_seq_length})"
        )

        # 6. Connect and validate each configured table. Dispose the engine
        # (and its pool) in a finally so no pooled connections are left open.
        try:
            engine = get_engine(db_name)
        except ValueError as e:
            logger.error(f"Invalid configuration: {e}")
            sys.exit(1)

        try:
            all_failures: list[str] = []
            insp = inspect(engine)

            for source_table, table_config in tables.items():
                embedding_table = table_config.get(
                    "embedding_table", f"{source_table}_embedding"
                )
                max_tokens = table_config.get("max_tokens", default_max_tokens)
                source_filter = table_config.get("source_filter", None)

                # The generator no longer accepts source_table_pks; a config
                # still carrying it would make this validator join on columns
                # the generator never used, so the key is ignored with a
                # visible warning.
                if "source_table_pks" in table_config:
                    logger.warning(
                        f"Table {source_table!r}: ignoring obsolete "
                        "'source_table_pks' config key; primary keys are "
                        "always auto-detected"
                    )

                # A missing source table must be a clean FAIL, not a raw
                # NoSuchTableError traceback from the PK inspection below.
                if not insp.has_table(source_table, schema=db_schema):
                    all_failures.append(
                        f"FAIL: source table {db_schema}.{source_table} does not exist"
                    )
                    continue

                # PK columns are ALWAYS auto-detected, reusing the generator's
                # own helper so the validator joins on exactly the columns the
                # generator used.
                try:
                    pk_columns = _detect_primary_keys(engine, db_schema, source_table)
                except ConfigurationError as e:
                    all_failures.append(f"FAIL: {e}")
                    continue

                logger.info(
                    f"Validating {db_schema}.{source_table} -> {embedding_table} "
                    f"(pks={pk_columns}, max_tokens={max_tokens})"
                )

                try:
                    failures = validate_embeddings(
                        engine=engine,
                        db_schema=db_schema,
                        source_table=source_table,
                        embedding_table=embedding_table,
                        pk_columns=pk_columns,
                        max_tokens=max_tokens,
                        max_seq_length=max_seq_length,
                        expected_dimension=expected_dimension,
                        count_tokens=count_tokens,
                        source_filter=source_filter,
                    )
                except ValueError as e:
                    logger.error(f"Invalid configuration: {e}")
                    sys.exit(1)
                except SQLAlchemyError as e:
                    logger.error(f"Database error validating {source_table}: {e}")
                    sys.exit(1)

                all_failures.extend(failures)
        finally:
            engine.dispose()

        finish_run(
            all_failures,
            success_message=(
                "OUTPUT VALIDATION PASSED: all embedding invariants hold"
            ),
            failure_prefix="OUTPUT VALIDATION FAILED",
        )


if __name__ == "__main__":
    main()
