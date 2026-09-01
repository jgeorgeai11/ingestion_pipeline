-- Schema template: document storage for RAG retrieval
--
-- This is a template used by the ingestion script (ingest.py).
-- Placeholders {schema_name}, {document_table}, and {content_table}
-- are replaced at runtime with values from the TOML config.
--
-- Identity is collection_path (a sanitized ltree), authored per document in
-- the TOML config. document.collection_path is the primary key and
-- document_content.collection_path is a cascading foreign key onto it.
--
-- Migration caveat: the create statements below are IF NOT EXISTS, so a re-run
-- against a schema whose tables already exist is a complete no-op for those
-- tables. Anything ADDED here after first deployment — a CHECK constraint, a
-- column, a default — therefore never reaches an existing environment and must
-- be applied there by a manual ALTER TABLE. The COMMENT ON statements at the
-- bottom are the exception: they run unconditionally and refresh on every run,
-- so refreshed comments are not evidence that constraints are in sync.

-- The ltree extension is REQUIRED but deliberately not created here:
-- installing an extension is a one-time provisioning act (superuser), not a
-- per-run step. The loader verifies it up front via
-- ingpipe_lib.db.require_extensions and fails actionably when missing.

create schema if not exists {schema_name};

-- Document table: one row per ingested document, keyed by collection_path.
-- CHECK constraints mirror the CleanedDocument/Document Pydantic invariants so
-- the same rules are enforced at rest (defense in depth against any path that
-- bypasses the load-step validation).
create table if not exists {schema_name}.{document_table} (
    collection_path ltree primary key,
    -- Reject blank/whitespace-only titles: title is the human-readable
    -- reference, and NOT NULL alone still admits ''.
    title text not null
        check (trim(title) <> ''),
    n_parsed_sections integer not null
        check (n_parsed_sections >= 1),
    -- Source provenance: Docling's origin.binary_hash (low 64 bits of
    -- sha256(source bytes)). An unsigned 64-bit value whose top half exceeds
    -- bigint's signed max, so numeric(20,0) is used. NOT NULL because every
    -- loaded document carries it (origin is always present for file-based
    -- parsing; a missing origin is treated as an error upstream).
    source_binary_hash numeric(20,0) not null
        check (source_binary_hash >= 0
               and source_binary_hash < 18446744073709551616),
    ingested_at timestamptz not null default now()
);

-- Document content table: one row per parsed section, keyed by
-- (collection_path, sort_order). Sections cascade-delete with their document.
-- CHECK constraints mirror the Section Pydantic invariants. Cross-row
-- contiguity of sort_order is enforced at load/validation time (not expressible
-- as a single-row CHECK); the per-row checks below cover the rest.
create table if not exists {schema_name}.{content_table} (
    collection_path ltree not null
        references {schema_name}.{document_table} (collection_path)
        on delete cascade,
    sort_order integer not null
        check (sort_order >= 1),
    heading_text text,
    content_text text,
    word_count integer not null
        check (word_count >= 0),
    page_start integer check (page_start >= 1),
    page_end integer check (page_end >= 1),
    primary key (collection_path, sort_order),
    -- A section must carry a REAL heading or REAL content: nullif(trim(...))
    -- folds '' and whitespace-only into NULL, so a section of two empty
    -- strings can no longer satisfy the guard (strengthens the former
    -- both-null check, which '' technically passed).
    check (nullif(trim(heading_text), '') is not null
           or nullif(trim(content_text), '') is not null),
    -- Page ordering when both are present (Section: page_start <= page_end).
    check (page_start is null or page_end is null or page_start <= page_end)
);

-- Generic structural descriptions, exposed via pg_catalog.obj_description()
-- and surfaced by the MCP server's list_tables tool. COMMENT ON overwrites,
-- so every run refreshes the text (idempotent). Corpus-flavored overrides,
-- when configured ([load].document_table_comment / content_table_comment),
-- are applied by ensure_schema AFTER this template runs and win.
--
-- The schema itself is deliberately NOT commented here, even though
-- ensure_schema accepts a third override ([load].schema_comment): a schema
-- description is per-corpus flavor with no sensible generic text, and several
-- configs load into a shared, pre-existing schema whose description a baked-in
-- comment would overwrite on every run. Schema descriptions therefore come
-- only from [load].schema_comment; a config that omits it leaves the schema's
-- existing description (or NULL) alone.
--
-- Each literal below is split across lines: PostgreSQL concatenates adjacent
-- string constants separated by whitespace containing a newline, so the stored
-- comment is one string while every source line stays within the length limit.
comment on table {schema_name}.{document_table} is
    'One row per ingested document, identified by collection_path '
    '(an ltree). Holds title (human-readable reference), '
    'n_parsed_sections, source_binary_hash, and ingested_at';

comment on table {schema_name}.{content_table} is
    'Section-level text of each document — one row per section keyed '
    'by (collection_path, sort_order), with heading_text, '
    'content_text, word_count, and page_start/page_end provenance. '
    'Use for reading or keyword-filtering document text';
