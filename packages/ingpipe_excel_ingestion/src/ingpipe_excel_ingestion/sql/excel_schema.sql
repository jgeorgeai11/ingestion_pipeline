-- Schema template: consolidated Excel storage (the universal embedding leg).
--
-- Used by ingest_excel.py. Placeholders {schema_name}, {sheet_table}, and
-- {content_table} are substituted at runtime with config values that the caller
-- has validated via validate_sql_identifier. Identity is collection_path (a
-- sanitized-or-authored ltree, one per sheet), matching ingpipe_file_ingestion's
-- document/document_content model. Every sheet is written here; a sheet that
-- also names a `table` is additionally written to that structured table
-- (created dynamically by structured_table.py — no DDL for those here).
--
-- CHECK constraints mirror the application-level invariants as defense in depth
-- (the load step writes columns directly), matching ingpipe_file_ingestion's schema.
--
-- NOTE: these statements are idempotent (IF NOT EXISTS) but do NOT migrate an
-- existing table. Upgrading a schema created before the collection_path rework
-- requires dropping and recreating the tables (or a separate migration) — the
-- new columns/constraints are not added to a pre-existing table. Same caveat as
-- ingpipe_file_ingestion.

-- The ltree extension is REQUIRED but deliberately not created here:
-- installing an extension is a one-time provisioning act (superuser), not a
-- per-run step. The ingester verifies it up front via
-- ingpipe_lib.db.require_extensions and fails actionably when missing.

create schema if not exists {schema_name};

-- Sheet table: one row per ingested sheet, keyed by collection_path.
create table if not exists {schema_name}.{sheet_table} (
    collection_path ltree primary key,
    -- Reject blank/whitespace-only titles: title is the human-readable
    -- reference, and NOT NULL alone still admits '' (a config-authored
    -- title = "" would otherwise be stored). Mirrors ingpipe_file_ingestion's
    -- document.title check, preserving the deliberate schema symmetry.
    title text not null
        check (trim(title) <> ''),
    n_rows integer not null
        check (n_rows >= 1),
    -- Per-sheet content fingerprint: low 64 bits of sha256 over the sheet's
    -- ordered row_text values. An unsigned 64-bit value whose top half exceeds
    -- bigint's signed max, so numeric(20,0) is used (same as ingpipe_file_ingestion).
    source_binary_hash numeric(20,0) not null
        check (source_binary_hash >= 0
               and source_binary_hash < 18446744073709551616),
    -- Informational only: the structured table this sheet also feeds, if any
    -- (NULL for embed-only sheets). Structured routing lives per-sheet in config.
    structured_table text,
    ingested_at timestamptz not null default now()
);

-- Sheet content table: one row per data row, keyed by
-- (collection_path, sort_order). Content rows cascade-delete with their sheet.
create table if not exists {schema_name}.{content_table} (
    collection_path ltree not null
        references {schema_name}.{sheet_table} (collection_path)
        on delete cascade,
    sort_order integer not null
        check (sort_order >= 1),
    row_text text not null
        check (row_text <> ''),
    word_count integer not null
        check (word_count >= 0),
    primary key (collection_path, sort_order)
);

-- Generic structural descriptions, exposed via pg_catalog.obj_description()
-- and surfaced by the MCP server's list_tables tool. COMMENT ON overwrites,
-- so every run refreshes the text (idempotent). Corpus-flavored overrides,
-- when configured (sheet_table_comment / content_table_comment), are applied
-- by ingest_excel.py AFTER this template runs and win.
-- Each description is written as adjacent string literals split across lines
-- (the parser concatenates literals separated by whitespace containing a
-- newline), purely to respect the 100-character line limit: the stored text is
-- one string, so the trailing space on each fragment is significant.
comment on table {schema_name}.{sheet_table} is
    'Catalog of ingested Excel worksheets — one row per sheet (collection_path) '
    'with title, n_rows, structured_table (the structured table it was loaded '
    'into, if any), source_binary_hash, ingested_at';

comment on table {schema_name}.{content_table} is
    'Generic row-level text of every ingested worksheet — one row per '
    'spreadsheet row (collection_path, sort_order) as newline key-value '
    'row_text; use for keyword search across all workbook data';
