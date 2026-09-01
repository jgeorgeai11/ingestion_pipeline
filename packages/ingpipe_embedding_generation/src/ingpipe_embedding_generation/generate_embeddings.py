"""Generate embeddings for source tables in PostgreSQL.

Reads rows from configurable source tables, chunks long text, generates
embeddings using a SentenceTransformer model, and inserts them into
dynamically created embedding tables. Multiple tables can be processed
in a single run. All table names, columns, and PKs are driven by TOML
config, with per-table overrides for shared defaults.
"""

import os
import sys
from pathlib import Path

# Allow MPS fallback to CPU for unsupported ops
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import torch
from ingpipe_lib.cli import (
    build_parser,
    finish_run,
    load_config,
    run_scope,
    setup_entry_logging,
)
from ingpipe_lib.db import get_engine, require_extensions
from ingpipe_lib.logconfig import get_logger
from ingpipe_lib.sql_comments import COMMENT_TEXT_PARAM, build_comment_statements
from sentence_transformers import SentenceTransformer
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.types import NullType

from ingpipe_embedding_generation._utils import make_token_counter, validate_sql_identifier
from ingpipe_embedding_generation.chunker import SectionChunk, SectionInput, chunk_long_sections

logger = get_logger(__name__)


class ConfigurationError(Exception):
    """Raised when required configuration is missing or invalid."""


# Global model cache (lazy loaded)
_model: SentenceTransformer | None = None
_model_name: str | None = None


def _get_embedding_model(model_name: str) -> SentenceTransformer:
    """Get or lazily load the SentenceTransformer embedding model.

    The model is config-driven (any SentenceTransformer-compatible HuggingFace
    model name); loading uses the library defaults, so a model requiring
    remote code execution would fail rather than silently trusting it.
    Selects the MPS device when available (dev Macs), else CPU (the
    deployment VM).

    Args:
        model_name: HuggingFace model name from the run's config
            (e.g., "ibm-granite/granite-embedding-small-english-r2").

    Returns:
        Loaded SentenceTransformer model instance.
    """
    global _model, _model_name
    if _model is None or _model_name != model_name:
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        logger.info(f"Loading embedding model: {model_name} (device: {device})")
        _model = SentenceTransformer(model_name, device=device)
        _model_name = model_name
        logger.info(
            f"Model loaded: dimension={_model.get_sentence_embedding_dimension()}"
        )
    return _model


def _validate_config_identifiers(
    db_schema: str,
    source_table: str,
    embedding_table: str,
    embed_columns: list[str],
    pk_columns: list[str],
    header_columns: list[str] | None = None,
) -> None:
    """Validate all table and column names from config are safe SQL identifiers.

    Args:
        db_schema: PostgreSQL schema name.
        source_table: Source table name.
        embedding_table: Embedding table name.
        embed_columns: Columns to embed.
        pk_columns: Primary key columns.
        header_columns: Optional contextual-header columns; validated because
            they are interpolated into the SELECT column list.

    Raises:
        ValueError: If any identifier is unsafe.
    """
    validate_sql_identifier(db_schema, "db_schema")
    validate_sql_identifier(source_table, "source_table")
    validate_sql_identifier(embedding_table, "embedding_table")
    for col in embed_columns:
        validate_sql_identifier(col, f"embed_column '{col}'")
    for col in pk_columns:
        validate_sql_identifier(col, f"pk_column '{col}'")
    for col in header_columns or []:
        validate_sql_identifier(col, f"header_column '{col}'")


def _detect_primary_keys(engine: Engine, db_schema: str, source_table: str) -> list[str]:
    """Auto-detect primary key columns from the source table via SQLAlchemy inspect.

    Args:
        engine: SQLAlchemy Engine connected to the target database.
        db_schema: PostgreSQL schema name.
        source_table: Source table name.

    Returns:
        List of primary key column names.

    Raises:
        ConfigurationError: If no primary keys are found.
    """
    insp = inspect(engine)
    pk_columns = insp.get_pk_constraint(source_table, schema=db_schema).get("constrained_columns", [])

    if not pk_columns:
        raise ConfigurationError(
            f"No primary key found for {db_schema}.{source_table}; cannot embed "
            "it. The embedding table needs the source's unique key for its "
            "composite PK and its foreign key, so a table without a PRIMARY KEY "
            "is not embeddable."
        )

    logger.info(f"Auto-detected PKs for {db_schema}.{source_table}: {pk_columns}")
    return pk_columns


def _verify_source_table_exists(engine: Engine, db_schema: str, source_table: str) -> None:
    """Verify the source table exists in the database.

    Args:
        engine: SQLAlchemy Engine connected to the target database.
        db_schema: PostgreSQL schema name.
        source_table: Source table name.

    Raises:
        ConfigurationError: If the source table does not exist.
    """
    insp = inspect(engine)
    if not insp.has_table(source_table, schema=db_schema):
        raise ConfigurationError(
            f"Source table '{db_schema}.{source_table}' does not exist. "
            "Ensure the source data has been ingested before generating embeddings."
        )
    logger.info(f"Source table verified: {db_schema}.{source_table}")


def _resolve_pg_column_type(
    engine: Engine, db_schema: str, source_table: str, col_name: str
) -> str:
    """Return the canonical Postgres type string for a source column.

    Used as a fallback when SQLAlchemy reflects a column as ``NullType`` (e.g.
    custom types like ``ltree`` it does not recognize). ``format_type`` returns
    the exact DDL spelling (``ltree``, ``integer``, ``character varying(255)``),
    so the mirrored PK column matches the source column's type and the FK stays
    valid. ``information_schema.data_type`` is unsuitable here because it reports
    ``USER-DEFINED`` for such types.

    Args:
        engine: SQLAlchemy Engine connected to the target database.
        db_schema: PostgreSQL schema holding the source table.
        source_table: Source table name.
        col_name: Column whose type to resolve.

    Returns:
        The canonical Postgres type string for the column.
    """
    query = text(
        """
        select format_type(a.atttypid, a.atttypmod)
        from pg_attribute a
        join pg_class c on c.oid = a.attrelid
        join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = :schema and c.relname = :tbl
            and a.attname = :col and a.attnum > 0 and not a.attisdropped
        """
    )
    with engine.connect() as conn:
        return conn.execute(
            query, {"schema": db_schema, "tbl": source_table, "col": col_name}
        ).scalar_one()


def _create_embedding_table(
    engine: Engine,
    db_schema: str,
    source_table: str,
    embedding_table: str,
    pk_columns: list[str],
    embedding_dimension: int,
    table_comment: str,
) -> None:
    """Create the embedding table dynamically if it does not exist.

    Creates a table with PK columns mirrored from the source table, plus
    chunk_number, chunk_text, word_count, and embedding columns. Adds a
    composite FK to the source table with ON DELETE CASCADE and an HNSW
    index on the embedding column.

    The table description is applied with COMMENT ON TABLE in the same
    transaction, unconditionally (COMMENT ON overwrites), so a re-run
    refreshes the text even when the table already exists. The text is data,
    bound as a parameter so the driver quotes it safely.

    Args:
        engine: SQLAlchemy Engine connected to the target database.
        db_schema: PostgreSQL schema name.
        source_table: Source table name (for FK reference).
        embedding_table: Embedding table name to create.
        pk_columns: Primary key column names from the source table.
        embedding_dimension: Dimension of the embedding vectors.
        table_comment: COMMENT ON TABLE description (the caller resolves the
            config override or the header-aware default).

    Raises:
        ConfigurationError: If a derived index name would exceed PostgreSQL's
            63-byte identifier limit. `create index if not exists` matches on
            the TRUNCATED name, so an over-long name could silently match a
            different index and leave the table without its own — failing
            loudly is the only safe behavior.
    """
    # Guard the derived index names against PostgreSQL's NAMEDATALEN-1
    # (63-byte) silent truncation before any DDL runs.
    for index_name in (
        f"idx_{embedding_table}_embedding_hnsw",
        f"idx_{embedding_table}_chunk_tsv",
    ):
        if len(index_name.encode("utf-8")) > 63:
            raise ConfigurationError(
                f"Derived index name {index_name!r} is "
                f"{len(index_name.encode('utf-8'))} bytes, over PostgreSQL's "
                "63-byte identifier limit; `create index if not exists` would "
                "silently match a truncated name. Configure a shorter "
                f"embedding_table (got {embedding_table!r}, "
                f"{len(embedding_table)} chars; max "
                f"{63 - len('idx__embedding_hnsw')})"
            )

    # Retrieve PK column types from the source table to mirror them
    insp = inspect(engine)
    source_columns = {col["name"]: col for col in insp.get_columns(source_table, schema=db_schema)}

    pk_col_defs = []
    for col_name in pk_columns:
        col_type_obj = source_columns[col_name]["type"]
        # SQLAlchemy reflects custom types (e.g. ltree) as NullType, which cannot
        # compile to DDL; fall back to the canonical Postgres type so the mirrored
        # PK column matches the source and the FK is valid.
        if isinstance(col_type_obj, NullType):
            col_type = _resolve_pg_column_type(engine, db_schema, source_table, col_name)
        else:
            col_type = col_type_obj.compile(dialect=engine.dialect)
        pk_col_defs.append(f"    {col_name} {col_type} not null")

    pk_cols_csv = ", ".join(pk_columns)
    pk_col_defs_str = ",\n".join(pk_col_defs)

    # Build composite PK: source PKs + chunk_number
    composite_pk = f"{pk_cols_csv}, chunk_number"

    # FK references the source table's PKs for cascade deletes
    fk_constraint = (
        f"    foreign key ({pk_cols_csv}) references {db_schema}.{source_table} ({pk_cols_csv}) "
        f"on delete cascade"
    )

    # The vector extension is REQUIRED but deliberately not created here:
    # installing an extension is a one-time provisioning act (superuser), not
    # a per-run step — pgvector is not even a trusted extension, so a database
    # owner cannot create it. generate_embeddings verifies it up front via
    # ingpipe_lib.db.require_extensions and fails actionably when missing.
    ddl = f"""
create table if not exists {db_schema}.{embedding_table} (
{pk_col_defs_str},
    chunk_number integer not null,
    chunk_text text not null,
    word_count integer not null,
    embedding vector({embedding_dimension}),
    chunk_tsv tsvector generated always as (to_tsvector('english', chunk_text)) stored,
    primary key ({composite_pk}),
{fk_constraint}
);

create index if not exists idx_{embedding_table}_embedding_hnsw
    on {db_schema}.{embedding_table}
    using hnsw (embedding vector_cosine_ops);

create index if not exists idx_{embedding_table}_chunk_tsv
    on {db_schema}.{embedding_table}
    using gin (chunk_tsv);
"""

    # The COMMENT ON statement comes from the shared ingpipe_lib builder
    # (see it for the psycopg2 client-side binding the :comment_text parameter
    # depends on) so every leg emits identical statements.
    comment_statements = build_comment_statements(
        db_schema, table_comments={embedding_table: table_comment}
    )

    with engine.begin() as conn:
        conn.execute(text(ddl))
        for statement, comment_text_value in comment_statements:
            conn.execute(text(statement), {COMMENT_TEXT_PARAM: comment_text_value})

    logger.info(
        f"Embedding table ensured: {db_schema}.{embedding_table} "
        f"(PKs: {pk_columns}, dimension: {embedding_dimension}, comment applied)"
    )


def _build_embed_text(row: dict[str, object], embed_columns: list[str]) -> str:
    r"""Build the text string used to generate an embedding by concatenating column values.

    A ``None`` value renders as an EMPTY value (``"col: "``) rather than
    omitting the line: a stable field layout across rows means the embedding
    model sees a consistent schema — the same deliberate choice as
    ``ingest_excel.build_row_text``, so the shared "col: value" format is
    actually shared across the legs.

    Args:
        row: Dict of column name to value for a single source row.
        embed_columns: Ordered list of column names whose values to concatenate.

    Returns:
        Column names and values as newline-delimited key-value lines
        (e.g. "col: val\ncol: val") — the format research favours for tabular
        RAG (column labels for field context, newline separator).
    """
    parts = []
    for col in embed_columns:
        value = row.get(col, "")
        parts.append(f"{col}: {'' if value is None else value}")
    return "\n".join(parts)


def _row_values_all_empty(row: dict[str, object], columns: list[str]) -> bool:
    """Return whether every named column's value is NULL or whitespace-only.

    Used to detect the row shape that carries nothing embeddable: with the
    stable "col: " rendering, the rendered text is never empty, so emptiness
    must be judged on the underlying VALUES across the embedded and header
    columns together.

    Args:
        row: Dict of column name to value for a single source row.
        columns: The embed + header columns to inspect.

    Returns:
        True when no named column holds a non-blank value.
    """
    return all(
        row.get(col) is None or not str(row.get(col)).strip() for col in columns
    )


def _build_source_filter_clause(
    source_filter: dict[str, str | list[str]] | None,
    table_alias: str | None = None,
) -> tuple[str, dict[str, str]]:
    """Build a SQL WHERE clause from source_filter config.

    Each key is a column name (validated as a safe SQL identifier). Each
    value is a string or list of strings used as LIKE patterns. Multiple
    values for the same column are ORed; multiple columns are ANDed.

    Args:
        source_filter: Mapping of column names to LIKE pattern(s), or None.
        table_alias: Optional table alias to prefix column names with
            (e.g., "src" produces "src.col_name like ...").

    Returns:
        Tuple of (where_clause, params) where where_clause is the bare
        condition string (no WHERE keyword) and params is a dict of
        bind-parameter names to values. Returns ("", {}) when the filter
        is None or empty.

    Raises:
        ValueError: If a column name fails SQL identifier validation.
    """
    if not source_filter:
        return ("", {})

    clause_parts: list[str] = []
    params: dict[str, str] = {}

    for col_name, patterns in source_filter.items():
        validate_sql_identifier(col_name, f"source_filter column '{col_name}'")

        # Normalize scalar to list
        if isinstance(patterns, str):
            patterns = [patterns]

        # Prefix column name with table alias when provided
        qualified_col = f"{table_alias}.{col_name}" if table_alias else col_name

        like_parts: list[str] = []
        for i, pattern in enumerate(patterns):
            param_name = f"sf_{col_name}_{i}"
            # Cast the column to text so LIKE works on non-text columns too —
            # notably the common `collection_path` identity, which is an ``ltree``
            # and has no LIKE operator (a no-op cast for text columns).
            like_parts.append(f"{qualified_col}::text like :{param_name}")
            params[param_name] = pattern

        if len(like_parts) == 1:
            clause_parts.append(like_parts[0])
        else:
            clause_parts.append(f"({' or '.join(like_parts)})")

    return (" and ".join(clause_parts), params)


def generate_embeddings(
    db_name: str,
    db_schema: str,
    source_table: str,
    embedding_table: str,
    embed_columns: list[str],
    model_name: str,
    batch_size: int,
    max_tokens: int,
    overlap_tokens: int,
    overwrite: bool,
    source_filter: dict[str, str | list[str]] | None = None,
    header_columns: list[str] | None = None,
    table_comment: str | None = None,
) -> int:
    r"""Generate and insert embeddings for source rows that lack them.

    Streams the rows without corresponding embeddings (a server-side cursor over
    the left-anti-join SELECT), chunks each row's long text to fit the embedding
    model's context window as the row arrives, and inserts each batch of chunks
    with a single executemany. Peak memory is therefore bounded by batch_size
    chunks plus the row in flight, not by the size of the un-embedded set — which
    on a model change is the whole corpus. Idempotent on re-run because it skips
    rows that already have embeddings (unless overwrite=True, which deletes
    matching embeddings first). Each source row's chunks commit atomically within
    one batch, so a partial failure leaves only whole rows un-embedded (correctly
    regenerated on re-run) — never a row with only some of its chunks.

    When source_filter is provided, the overwrite DELETE, the count of rows
    needing embeddings, and the streamed SELECT are all scoped to matching rows
    only. The overwrite DELETE commits before the inserts begin (a deliberate,
    documented window — see the inline comment at the DELETE), and an
    interrupted overwrite run is healed by the next run's anti-join. The
    engine created for the table is disposed in a ``finally`` so a multi-table
    run does not leak pooled connections.

    Args:
        db_name: PostgreSQL database name.
        db_schema: PostgreSQL schema name.
        source_table: Name of the source table to read from.
        embedding_table: Name of the embedding table to write to.
        embed_columns: Column names whose values are concatenated for embedding
            as newline key-value lines ("col: value\ncol: value").
        model_name: HuggingFace model name from the run's config (any
            SentenceTransformer-compatible model, e.g.
            "ibm-granite/granite-embedding-small-english-r2").
        batch_size: Number of chunks to encode per model.encode() call and insert
            per transaction; also the streamed read's fetch window (yield_per), so
            it sets the run's memory ceiling. A row whose chunks exceed it becomes
            its own larger batch rather than being split.
        max_tokens: Maximum model tokens per chunk before splitting.
        overlap_tokens: Token overlap between adjacent chunks.
        overwrite: If True, delete existing embeddings for matching source rows before regenerating.
        source_filter: Optional mapping of column names to LIKE pattern(s) for
            filtering source rows. Multiple values per column are ORed; columns
            are ANDed.
        header_columns: Optional columns whose "col: value\n..." text is a
            contextual header prepended to EVERY chunk of a row (so tail chunks
            of long sections keep their heading in the vector, chunk_text, and
            FTS). The header's token cost is reserved from max_tokens so the
            body is chunked with a reduced budget; stored word_count remains the
            body chunk's count. When None or empty, behavior is exactly as when
            the whole row is chunked without a header.
        table_comment: Optional COMMENT ON TABLE description for the embedding
            table. When None, a default is built from the source table name,
            conditional on ``header_columns`` so the text never claims a
            header prefix the config does not produce. Applied (refreshed) on
            every run.

    Returns:
        Number of embeddings generated and inserted.

    Raises:
        ConfigurationError: If table_comment is not a string, if source_filter
            references columns not on the source table, if the source table
            does not exist, or if the source table has no primary key (the
            last two propagate from the pre-flight helpers).
        ValueError: If any config-supplied identifier is unsafe — the schema,
            table, embed/header column names, or a source_filter key; if a
            required POSTGRES_* environment variable is missing or the port is
            not an integer (from the shared engine factory); or if the vector
            extension is not installed in the target database (from the
            extension preflight).
        SQLAlchemyError: If any of the DDL, DELETE, SELECT, or INSERT
            statements fail against the database.
    """
    header_columns = header_columns or []

    # Resolve the embedding table's description up front: a config override
    # wins; otherwise the default describes the hybrid model, claiming the
    # header prefix only when header_columns actually produce one.
    if table_comment is not None and not isinstance(table_comment, str):
        raise ConfigurationError(
            f"table_comment must be a string, got {table_comment!r}"
        )
    if table_comment is None:
        chunk_kind = "Header-prefixed chunks" if header_columns else "Chunks"
        table_comment = (
            f"{chunk_kind} of {db_schema}.{source_table} for hybrid retrieval "
            "(embedding HNSW + chunk_tsv GIN); backs the search tool"
        )

    engine = get_engine(db_name)

    # Everything below runs under a try/finally that disposes this
    # per-table engine: without it, an N-table run leaks N pooled
    # connection sets for the process lifetime.
    try:

        # Extension contract: provisioning installs pgvector, the engine only
        # verifies it — failing here, before any DDL, with an actionable message
        # rather than a privilege error mid-transaction (vector is not a trusted
        # extension, so even the database owner cannot create it).
        require_extensions(engine, ["vector"])

        # Pre-flight: verify source table exists
        _verify_source_table_exists(engine, db_schema, source_table)

        # Determine PK columns by auto-detecting the source table's primary key.
        pk_columns = _detect_primary_keys(engine, db_schema, source_table)

        # Validate all identifiers after PK resolution
        _validate_config_identifiers(
            db_schema, source_table, embedding_table, embed_columns, pk_columns, header_columns
        )

        # Validate source_filter column names exist on the source table
        if source_filter:
            insp = inspect(engine)
            source_col_names = {col["name"] for col in insp.get_columns(source_table, schema=db_schema)}
            invalid_cols = set(source_filter.keys()) - source_col_names
            if invalid_cols:
                raise ConfigurationError(
                    f"source_filter references columns not on {db_schema}.{source_table}: {sorted(invalid_cols)}"
                )

        # Build source filter clause (empty string + empty dict when no filter)
        filter_clause, filter_params = _build_source_filter_clause(source_filter)

        # Load model to get embedding dimension for table creation. The
        # library types the dimension as optional; a model that cannot report
        # it cannot drive the vector(N) DDL, so fail actionably.
        model = _get_embedding_model(model_name)
        embedding_dim = model.get_sentence_embedding_dimension()
        if embedding_dim is None:
            raise ConfigurationError(
                f"Model {model_name!r} reports no embedding dimension; cannot "
                "create the vector column"
            )

        # Token counter from the model's own tokenizer; the chunker budgets chunks
        # the way the model counts (special tokens included), so chunks never exceed
        # the model's context window and are not silently truncated at encode time.
        count_tokens = make_token_counter(model)

        # Create embedding table dynamically (idempotent — uses IF NOT EXISTS);
        # its COMMENT ON description is applied/refreshed on every run.
        _create_embedding_table(
            engine, db_schema, source_table, embedding_table, pk_columns,
            embedding_dim, table_comment,
        )

        # Overwrite mode: delete embeddings for matching source rows, scoped by
        # source_filter.
        #
        # DELIBERATE WINDOW (recorded decision): the delete commits in its own
        # transaction before any insert, so between this commit and the last
        # insert batch the matching embeddings are absent. Closing the window
        # would require the streamed anti-join SELECT (its own connection) to
        # see an uncommitted delete, or one corpus-sized transaction held for
        # the whole run — both worse than the window. The window is safe for
        # this single-writer pipeline: a run that dies mid-way leaves the
        # deleted rows un-embedded, which the PK anti-join re-detects and
        # regenerates on the next run.
        if overwrite:
            pk_cols_plain = ", ".join(pk_columns)
            subselect_where = f" where {filter_clause}" if filter_clause else ""
            delete_sql = (
                f"delete from {db_schema}.{embedding_table} "
                f"where ({pk_cols_plain}) in "
                f"(select {pk_cols_plain} from {db_schema}.{source_table}{subselect_where})"
            )
            with engine.begin() as conn:
                result = conn.execute(text(delete_sql), filter_params)
                logger.info(
                    f"Overwrite: deleted {result.rowcount} existing embeddings "
                    f"for source rows in {db_schema}.{embedding_table}"
                )

        # Columns to fetch per row: the chunked body (embed_columns) plus any
        # header columns not already in embed_columns (deduped, order-preserving so
        # the SELECT column list aligns with column_names for the zip below).
        value_columns = embed_columns + [c for c in header_columns if c not in embed_columns]

        # Select source rows that don't yet have embeddings (left join on PK columns)
        pk_cols_csv = ", ".join(f"src.{col}" for col in pk_columns)
        embed_cols_csv = ", ".join(f"src.{col}" for col in value_columns)
        join_conditions = " and ".join(f"src.{col} = emb.{col}" for col in pk_columns)

        # Build aliased filter clause directly with the source table alias
        aliased_filter_clause, _ = _build_source_filter_clause(source_filter, table_alias="src")
        select_filter = f" and {aliased_filter_clause}" if aliased_filter_clause else ""

        # Left-anti-join: a source row with no matching embedding yields NULL emb
        # columns, so `emb.{first pk} is null` (a PK column, otherwise NOT NULL)
        # selects exactly the rows that still need embeddings. The FROM/WHERE half is
        # shared with the count below so both queries see the identical filtered set.
        anti_join_source = (
            f"from {db_schema}.{source_table} as src "
            f"left join {db_schema}.{embedding_table} as emb "
            f"  on {join_conditions} "
            f"where emb.{pk_columns[0]} is null{select_filter}"
        )
        count_sql = f"select count(*) {anti_join_source}"
        select_sql = (
            f"select {pk_cols_csv}, {embed_cols_csv} {anti_join_source} "
            f"order by {pk_cols_csv}"
        )

        # Deliberate second query: the streamed read below never materializes its
        # result, so the row total behind the "Found N rows" line, the early return,
        # and the progress denominator comes from its own count over the same
        # filtered anti-join. Its cost is negligible beside embedding compute, and
        # only logging depends on it (the pipeline is single-writer, so the count and
        # the stream cannot disagree in practice).
        with engine.connect() as conn:
            total_rows: int = conn.execute(text(count_sql), filter_params).scalar_one()

        if not total_rows:
            logger.info("No rows need embeddings, skipping")
            return 0

        logger.info(f"Found {total_rows} rows needing embeddings")

        column_names = pk_columns + value_columns

        # Invariant insert SQL (PK columns are validated identifiers).
        pk_placeholders = ", ".join(f":{col}" for col in pk_columns)
        pk_col_names = ", ".join(pk_columns)
        insert_sql = (
            f"insert into {db_schema}.{embedding_table} "
            f"({pk_col_names}, chunk_number, chunk_text, word_count, embedding) "
            f"values ({pk_placeholders}, :chunk_number, :chunk_text, :word_count, cast(:embedding as vector))"
        )

        total_inserted = 0
        rows_inserted = 0

        def _flush_batch(
            batch: list[tuple[dict[str, object], SectionChunk, int]],
            batch_rows: int,
        ) -> None:
            """Encode and insert one batch of WHOLE rows in a single transaction.

            Args:
                batch: The batch's (pk_values, chunk_dict, chunk_number) tuples,
                    holding every chunk of each row it covers.
                batch_rows: Number of source rows the batch's chunks came from, used
                    for the run's row-progress logging.
            """
            nonlocal total_inserted, rows_inserted
            embed_texts = [chunk_dict["content_text"] for _, chunk_dict, _ in batch]
            embeddings = model.encode(embed_texts, show_progress_bar=False)
            params_list = [
                {
                    **pk_values,
                    "chunk_number": chunk_number,
                    "chunk_text": chunk_dict["content_text"],
                    "word_count": chunk_dict["word_count"],
                    # Serialize to pgvector literal format
                    "embedding": "[" + ",".join(map(str, embeddings[i].tolist())) + "]",
                }
                for i, (pk_values, chunk_dict, chunk_number) in enumerate(batch)
            ]
            with engine.begin() as conn:
                # One executemany round trip for the whole batch instead of one per
                # chunk. The transaction boundary is unchanged, so the batch's rows
                # still commit together (whole-row atomicity).
                conn.execute(text(insert_sql), params_list)
            total_inserted += len(batch)
            rows_inserted += batch_rows
            logger.info(
                f"Inserted batch of {len(batch)} embeddings from {batch_rows} rows "
                f"({rows_inserted}/{total_rows} rows, {total_inserted} chunks total)"
            )

        # Stream the un-embedded rows and flush as they are consumed, so peak memory
        # tracks batch_size chunks plus the row in flight rather than the whole
        # un-embedded corpus (the full re-embed case, where that set IS the corpus).
        # Rows are packed into batches of ~batch_size chunks, each committed in its
        # own transaction. Flush boundaries fall BETWEEN rows: a partial failure (a
        # later batch raises and is caught per-table in main()) leaves only whole rows
        # un-committed, which the PK-only "needs embeddings" left-join correctly
        # re-detects on rerun. A row is never left partially embedded, and a single
        # row larger than batch_size becomes its own (larger) batch, still atomic.
        batch: list[tuple[dict[str, object], SectionChunk, int]] = []
        batch_rows = 0
        rows_streamed = 0
        rows_skipped = 0

        with engine.connect() as conn:
            # yield_per implies stream_results, i.e. a psycopg2 server-side cursor
            # fetching a batch's worth of rows at a time (the same driver the
            # COMMENT ON binding above relies on). Connection.execution_options
            # applies in place and returns this same Connection.
            stream_conn = conn.execution_options(yield_per=max(batch_size, 1))
            result = stream_conn.execute(text(select_sql), filter_params)

            # Each row's chunks are built as the row arrives; the writes below take
            # their own pooled connection, so the cursor stays open across flushes.
            for row in result:
                # Extract PK values and embed column values from the row
                row_dict = dict(zip(column_names, row))
                pk_values = {col: row_dict[col] for col in pk_columns}
                rows_streamed += 1

                # Skip a row whose header AND body VALUES are all empty — the case
                # that previously stored an embedding of the empty string. Judged
                # on the values, not the rendered text: the stable "col: " layout
                # is never an empty string. A heading-only section (NULL body,
                # heading present) still embeds — the header prefix carries real
                # meaning into every chunk.
                if _row_values_all_empty(row_dict, value_columns):
                    rows_skipped += 1
                    logger.warning(
                        f"Row {pk_values} in {db_schema}.{source_table} has no "
                        "content in any embedded or header column; skipping"
                    )
                    continue

                # Build the body text (the chunked content) from embed_columns.
                body_text = _build_embed_text(row_dict, embed_columns)
                row_chunks: list[tuple[dict[str, object], SectionChunk, int]] = []

                if header_columns:
                    # Contextual chunk header: prepend the header columns' text to
                    # EVERY chunk so tail chunks of long sections keep their heading.
                    # Reserve the header's exact token cost (prefix plus the trailing
                    # newline joiner) so the body is chunked with the remaining
                    # budget; this keeps the combined header+body within max_tokens
                    # for real (splittable) content. The header text and its cost flow
                    # into both the embedding input and the stored chunk_text.
                    header_prefix = _build_embed_text(row_dict, header_columns) + "\n"
                    header_cost = count_tokens(header_prefix)
                    # Floor the budget above overlap_tokens so the chunker's
                    # overlap_tokens < max_tokens invariant always holds. Only
                    # triggers for pathologically long headers; real headings leave a
                    # wide margin.
                    effective_budget = max_tokens - header_cost
                    if effective_budget <= overlap_tokens:
                        logger.warning(
                            f"Header token cost ({header_cost}) leaves an effective body "
                            f"budget of {effective_budget} <= overlap_tokens ({overlap_tokens}); "
                            f"flooring body budget to {overlap_tokens + 1}"
                        )
                        effective_budget = overlap_tokens + 1

                    section_dicts: list[SectionInput] = [{"content_text": body_text}]
                    chunks = chunk_long_sections(
                        section_dicts, effective_budget, overlap_tokens, count_tokens
                    )

                    # Prepend the header to each chunk's text (used for both the
                    # embedding vector and the stored chunk_text). word_count is left
                    # as the chunker's body count — NOT recounted over header+body —
                    # so the validator's single-word warning still covers
                    # unsplittable separators.
                    for chunk_dict, chunk_number in chunks:
                        chunk_dict["content_text"] = header_prefix + chunk_dict["content_text"]
                        row_chunks.append((pk_values, chunk_dict, chunk_number))
                else:
                    # No header: chunk the body with the full token budget (behavior
                    # identical to chunking the whole row's embed text).
                    section_dicts2: list[SectionInput] = [{"content_text": body_text}]
                    chunks = chunk_long_sections(section_dicts2, max_tokens, overlap_tokens, count_tokens)

                    for chunk_dict, chunk_number in chunks:
                        row_chunks.append((pk_values, chunk_dict, chunk_number))

                # A row can produce zero chunks (whitespace-only text exceeding the
                # token budget — the chunker warns and emits nothing). Count and
                # name it here so the drop is visible in this run's summary rather
                # than silently counted as processed. Note the row is NOT marked
                # embedded, so the PK anti-join re-selects it on every future run.
                if not row_chunks:
                    rows_skipped += 1
                    logger.warning(
                        f"Row {pk_values} in {db_schema}.{source_table} produced no "
                        "chunks (whitespace-only text exceeding the token budget); "
                        "skipping"
                    )
                    continue

                # Flush BEFORE adding this row's chunks when they would overflow the
                # batch, so the row stays whole in the next batch.
                if batch and len(batch) + len(row_chunks) > batch_size:
                    _flush_batch(batch, batch_rows)
                    batch = []
                    batch_rows = 0
                batch.extend(row_chunks)
                batch_rows += 1

        # Final partial batch, flushed after the cursor is released.
        if batch:
            _flush_batch(batch, batch_rows)

        logger.info(
            f"Chunked {rows_streamed} rows into {total_inserted} chunks "
            f"(max_tokens={max_tokens}, overlap={overlap_tokens})"
        )
        # Surface skipped rows in the run summary at WARNING so the drop is never
        # silent; each skipped row was already named individually above.
        if rows_skipped:
            logger.warning(
                f"Skipped {rows_skipped}/{rows_streamed} row(s) that produced no "
                f"chunks in {db_schema}.{source_table}; they remain un-embedded "
                "and will be re-selected on the next run"
            )
        logger.info(f"Embedding generation complete: {total_inserted} embeddings inserted")
        return total_inserted
    finally:
        engine.dispose()


def validate_config(config: dict) -> None:
    """Validate the embedding config's top-level structure and value types.

    The single, named config gate this module and its output validator
    (``data_val_embeddings``) both call, so a config the generator accepts
    can never be one the validator rejects. Checks the required top-level
    fields (``db_name``/``db_schema``/``model_name`` strings, ``tables`` a
    non-empty table of per-table tables) and that a top-level ``overwrite``
    is a boolean (a quoted ``"false"`` is truthy and would trigger the
    destructive DELETE of existing embeddings).

    Per-table settings are deliberately NOT validated here: ``main()``
    records a malformed table entry as a failed table and continues with its
    siblings, and that resilience must not become an up-front abort.

    Args:
        config: Parsed TOML config dict.

    Raises:
        ValueError: If a required field is missing or a value has the wrong
            type (config-level errors that should abort the run).
    """
    for field in ("db_name", "db_schema", "model_name"):
        if field not in config:
            raise ValueError(f"Missing required config field: {field!r}")
        if not isinstance(config[field], str) or not config[field]:
            raise ValueError(
                f"{field} must be a non-empty string, got {config[field]!r}"
            )

    if "tables" not in config:
        raise ValueError("Missing required config field: 'tables'")
    tables = config["tables"]
    # `tables` must be a TOML table of per-table tables ([tables.<name>]).
    # Without this, a config written as `tables = ["a", "b"]` would fail with
    # an unhandled AttributeError on .items() instead of a clean config error.
    if not isinstance(tables, dict):
        raise ValueError(
            f"'tables' must be a table of per-table configs "
            f"(e.g. [tables.my_table]), got {type(tables).__name__}"
        )
    if not tables:
        raise ValueError("No tables specified in config")

    # overwrite is the one key whose wrong type changes behaviour silently
    # rather than raising: a quoted `overwrite = "false"` is a truthy string,
    # so the run would DELETE existing embeddings instead of resuming.
    overwrite = config.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise ValueError(f"overwrite must be a boolean, got {overwrite!r}")


def main() -> None:
    """Entry point for embedding generation script."""
    # Canonical --config/--env-file pair plus this script's --overwrite.
    parser = build_parser("Generate embeddings for source tables in PostgreSQL")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        default=None,
        help="Delete existing embeddings for source rows and regenerate (overrides TOML config)",
    )
    args = parser.parse_args()

    # INFO level, named from the config stem, anchored to the instance root
    # (after argparse so --help creates no log files).
    config_path = Path(args.config)
    setup_entry_logging("ingpipe_embedding_generation", config_path)

    with run_scope():
        # Resolve credentials and load the TOML config (exits 1 on a missing
        # env file, missing config, or malformed TOML).
        config = load_config(config_path, args.env_file)

        # Validate the top-level structure, then extract fields.
        try:
            validate_config(config)
        except ValueError as e:
            logger.error(f"Invalid config value: {e}")
            sys.exit(1)

        db_name = config["db_name"]
        db_schema = config["db_schema"]
        model_name = config["model_name"]
        tables = config["tables"]

        # Top-level defaults for optional fields
        default_batch_size = config.get("batch_size", 64)
        default_max_tokens = config.get("max_tokens", 500)
        default_overlap_tokens = config.get("overlap_tokens", 50)
        default_overwrite = config.get("overwrite", False)

        # CLI --overwrite flag overrides TOML value
        if args.overwrite is not None:
            default_overwrite = args.overwrite

        logger.info(
            f"Config loaded: db={db_name}, schema={db_schema}, "
            f"model={model_name}, tables={list(tables.keys())}"
        )

        # Process each table
        total_count = 0
        failed_tables: list[str] = []

        for source_table, table_config in tables.items():
            # Each entry must itself be a table of settings; anything else (a
            # bare string, list, or number) is recorded as a failed table
            # rather than raising on the membership test and lookups below.
            if not isinstance(table_config, dict):
                logger.error(
                    f"Table {source_table!r}: config must be a table of settings, "
                    f"got {type(table_config).__name__}"
                )
                failed_tables.append(source_table)
                continue

            # embed_columns is required per table
            if "embed_columns" not in table_config:
                logger.error(f"Table {source_table!r}: missing required field 'embed_columns'")
                failed_tables.append(source_table)
                continue

            # Per-table settings with top-level fallbacks
            embed_columns = table_config["embed_columns"]
            embedding_table = table_config.get("embedding_table", f"{source_table}_embedding")
            batch_size = table_config.get("batch_size", default_batch_size)
            max_tokens = table_config.get("max_tokens", default_max_tokens)
            overlap_tokens = table_config.get("overlap_tokens", default_overlap_tokens)
            overwrite = table_config.get("overwrite", default_overwrite)
            # Same non-boolean guard as the top-level key: a quoted "false" is
            # a truthy string that would trigger the destructive DELETE.
            # Recorded as a failed table so sibling tables still process.
            if not isinstance(overwrite, bool):
                logger.error(
                    f"Table {source_table!r}: overwrite must be a boolean, "
                    f"got {overwrite!r}"
                )
                failed_tables.append(source_table)
                continue
            source_filter = table_config.get("source_filter", None)
            header_columns = table_config.get("header_columns", [])
            table_comment = table_config.get("table_comment", None)

            logger.info(
                f"Processing table: {db_schema}.{source_table} -> {embedding_table}, "
                f"embed_columns={embed_columns}, header_columns={header_columns}, "
                f"batch_size={batch_size}, max_tokens={max_tokens}, overlap={overlap_tokens}, "
                f"overwrite={overwrite}, source_filter={source_filter}, "
                f"table_comment={'default' if table_comment is None else table_comment!r}"
            )

            try:
                count = generate_embeddings(
                    db_name=db_name,
                    db_schema=db_schema,
                    source_table=source_table,
                    embedding_table=embedding_table,
                    embed_columns=embed_columns,
                    model_name=model_name,
                    batch_size=batch_size,
                    max_tokens=max_tokens,
                    overlap_tokens=overlap_tokens,
                    overwrite=overwrite,
                    source_filter=source_filter,
                    header_columns=header_columns,
                    table_comment=table_comment,
                )
                total_count += count
                logger.info(f"Table {source_table}: {count} embeddings generated")
            except (ConfigurationError, ValueError, SQLAlchemyError) as e:
                logger.error(f"Table {source_table} failed: {e}", exc_info=True)
                failed_tables.append(source_table)

        # Summary, then the shared failure tail (each failed table at ERROR
        # with a counted summary, exiting 1 if any failed).
        logger.info(
            f"Embedding generation complete: {total_count} total embeddings "
            f"across {len(tables) - len(failed_tables)}/{len(tables)} tables"
        )
        finish_run(
            [f"Table failed: {name}" for name in failed_tables],
            success_message=f"SUCCESS: {total_count} embeddings across {len(tables)} table(s)",
            failure_prefix="FAILURE",
        )


if __name__ == "__main__":
    main()
