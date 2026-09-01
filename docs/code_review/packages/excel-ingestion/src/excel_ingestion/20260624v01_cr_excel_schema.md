---
name: cr-excel_schema
goal: Address SQL quality issues identified in code/excel_ingestion/sql/excel_schema.sql to align with sql-development core skills and general DDL correctness.
created: 2026-06-24
updated: 2026-06-24
---

## Implementation Plan

1. [completed] Idempotency / migration caveat - `code/excel_ingestion/sql/excel_schema.sql`
   - 1.1. [minor] Lines 19, 38: Both tables use `create table if not exists`, which is
        correctly idempotent for repeated runs but does NOT migrate a table that
        already exists with the OLD shape (pre-rework: `table_name`/`row_text`/`||`
        layout, no per-sheet `source_binary_hash`, old CHECK set). On a database
        carried over from before commit 7b5f236, the new columns/constraints are
        silently never applied and downstream inserts referencing
        `source_binary_hash` will fail at runtime. This mirrors file_ingestion's
        same limitation, so it is acceptable as designed, but the no-migration
        behaviour should be stated so an operator knows to drop/recreate (or write a
        migration) when upgrading an existing schema.
        - Current: `create table if not exists {schema_name}.{sheet_table} (...)`
          with no migration path for an existing table.
        - Expected: Add a header comment noting that `if not exists` does not ALTER
          an existing table — upgrading from the pre-rework schema requires dropping
          and recreating the tables (or a separate migration), consistent with
          file_ingestion.

## Skills with No Issues

1. Formatting (lowercase, 4-space indent, ≤100 chars): No issues found. All keywords
   lowercase; one column definition per line; CHECK clauses wrapped and indented
   under their column; no line exceeds 100 chars.
2. Comments (explain why / business rules): No issues found. The file header explains
   the placeholder substitution and validate-before-substitute contract; the
   `source_binary_hash` comment explains the uint64 / `numeric(20,0)` sizing rationale;
   the cascade and the per-sheet-vs-config routing of `structured_table` are explained.
3. DDL correctness: No issues found. `create extension if not exists ltree` and
   `create schema if not exists` precede the tables; `{sheet_table}` PK is
   `collection_path ltree primary key`; `{content_table}` FK references
   `{sheet_table}(collection_path)` `on delete cascade` with composite PK
   `(collection_path, sort_order)` — structurally analogous to file_ingestion's
   document/document_content.
4. CHECK constraints: No issues found and correct. `n_rows >= 1` (every sheet has at
   least one data row — the caller skips empty sheets), `source_binary_hash` bounded to
   `[0, 2^64)`, `sort_order >= 1`, `row_text <> ''`, `word_count >= 0` — each mirrors an
   application-level invariant as defense in depth, matching file_ingestion.
5. numeric(20,0) sizing for the uint64 hash: No issues found. An unsigned 64-bit value
   reaches `2^64 - 1`, exceeding bigint's signed max, and 2^64 has 20 decimal digits;
   `numeric(20,0)` holds the full range and the CHECK pins it to `[0, 2^64)`. Matches
   `compute_source_hash` (low-64-bit form) and file_ingestion's column.
6. PK / FK design + cascade: No issues found. Single-column ltree PK on the parent and
   cascading FK + composite PK on the child correctly model the one-sheet-to-many-rows
   relationship and make a sheet delete cascade to its content.
7. Placeholder / substitution soundness: No issues found. `{schema_name}`,
   `{sheet_table}`, `{content_table}` are the exact three placeholders the caller
   (`ensure_consolidated_tables`) substitutes; the caller validates each via
   `validate_sql_identifier` before substitution and raises on any leftover `{...}`, so
   DDL identifiers (which cannot be parameterized) are injection-safe.
8. Explicit columns / joins / CTEs / union: N/A - this is DDL, not a query.

## Status & Next Steps

**Current Status**: RESOLVED. 1.1: added a header NOTE that IF NOT EXISTS does not migrate an existing table (drop+recreate or migrate to upgrade), matching file_ingestion. Stale pre-rework cr_excel_schema.md deleted.
**Completed**:
1. Reviewed against sql-development best-practices and SKILL.md, plus general DDL
   correctness.
2. Verified CHECK constraints, PK/FK + cascade design, and `numeric(20,0)` sizing
   against the `compute_source_hash` / caller code and the file_ingestion analogue.
3. Confirmed placeholder substitution is sound and injection-safe (validate-before-
   substitute + leftover-placeholder guard in the caller).
**Next Steps**:
1. Optionally add the no-migration header note (finding 1).
**Blockers**:
1. None.
**Notes**:
1. No [critical]/[major] findings. `if not exists` makes re-runs safe but never ALTERs
   an existing table — the only operational caveat, shared with file_ingestion.
2. The stale `docs/code_review/excel_ingestion/cr_excel_schema.md` describes the
   PRE-rework schema (`table_name`/`row_text` columns) and is superseded by this dated
   review.
