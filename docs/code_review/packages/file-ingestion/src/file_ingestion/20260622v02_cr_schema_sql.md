---
name: cr-schema_sql
goal: Address code quality issues identified in code/file_ingestion/sql/schema.sql to align with sql-development best-practices and general DDL correctness.
created: 2026-06-22
updated: 2026-06-22
---

## Implementation Plan

1. [pending] DDL correctness / data integrity - `code/file_ingestion/sql/schema.sql`
   - 1.1. [suggestion] Lines 16-27: `document` has no CHECK constraints mirroring the application-level invariants enforced in `cleaned_models.Document`. `n_parsed_sections integer not null` permits negatives and zero, but the Pydantic model declares `n_parsed_sections: int = Field(ge=1)` and `CleanedDocument._check_invariants` rejects zero-section documents. `source_binary_hash numeric(20,0) not null` permits negatives and values above `2^64-1`, but the model declares `binary_hash: int = Field(ge=0)` and the value is an unsigned 64-bit integer. The DB is a second source of truth here (the load step writes directly), so consider DB-level CHECKs as defense-in-depth.
        - Current: `n_parsed_sections integer not null,` and `source_binary_hash numeric(20,0) not null,`
        - Expected: `n_parsed_sections integer not null check (n_parsed_sections >= 1),` and `source_binary_hash numeric(20,0) not null check (source_binary_hash >= 0 and source_binary_hash < 18446744073709551616),`
   - 1.2. [suggestion] Lines 31-42: `document_content` has no CHECK constraints mirroring `cleaned_models.Section`. The model enforces `word_count: int = Field(ge=0)`, `page_start`/`page_end` as `Field(ge=1)`, and `page_start <= page_end` when both are non-null; none of these are enforced in DDL. `word_count integer not null` allows negatives; `page_start`/`page_end` allow `0` or negative page numbers and allow `page_start > page_end`. Consider CHECKs so the page range / counts cannot be violated by a direct write.
        - Current: `word_count integer not null,` / `page_start integer,` / `page_end integer,`
        - Expected: add `check (word_count >= 0)`, `check (page_start is null or page_start >= 1)`, `check (page_end is null or page_end >= 1)`, and `check (page_start is null or page_end is null or page_start <= page_end)`.
   - 1.3. [suggestion] Lines 31-42: `document_content` does not enforce the "section must carry a heading or content" invariant that `Section._check_invariants` enforces (`heading_text` and `content_text` are never both NULL). Both columns are nullable with no table-level guard, so a `(NULL, NULL)` section row is accepted by the DB. A CHECK would close the gap.
        - Current: `heading_text text,` / `content_text text,` (both nullable, no guard)
        - Expected: add `check (heading_text is not null or content_text is not null)`.
   - **RESOLVED (1.1, 1.2, 1.3):** the full set of CHECK constraints was added to `schema.sql` — `document`: `n_parsed_sections >= 1`, `source_binary_hash` in `[0, 2^64)`; `document_content`: `sort_order >= 1`, `word_count >= 0`, `page_start >= 1`, `page_end >= 1`, `page_start <= page_end` (null-guarded), and the both-null `heading_text`/`content_text` guard. Verified: the cms_iom tables were dropped and recreated via `ensure_schema` with all 7 CHECKs present, and a both-null content insert is rejected. (Cross-row `sort_order` contiguity remains load/validation-time only — not expressible as a single-row CHECK.)
   - 1.4. [minor] Lines 18, 36-37: `title` (on `document`) and `heading_text`/`content_text` (on `document_content`) are nullable `text` with no length or non-empty constraint. This is consistent with the models (`title` is config-attached and `heading_text`/`content_text` are `str | None`), so it is acceptable, but worth noting that no minimum-length / non-blank check exists — an empty-string `''` heading is distinct from NULL and would pass. Only raise to a CHECK if the load step can emit `''`.

## Skills with No Issues

1. best-practices §7 (Formatting — lowercase, 4-space indent, <=100 chars): No issues found. All keywords, identifiers, and types are lowercase; indentation is 4 spaces; no line exceeds 100 chars.
2. best-practices §8 (Comments — explain why, on the line above, not inline): No issues found. The header block and the `source_binary_hash` provenance comment (lines 20-24) and the per-table comments (lines 15, 29-30) explain the why (collection_path identity, uint64 rationale, cascade semantics) and sit above the code.
3. best-practices §1-6, §9 (column references, joins, group/order by, union, CTEs, query-block annotation): N/A — this is a DDL template, not a SELECT/query, so the query-shaping conventions do not apply.
4. DDL correctness — ltree extension setup: No issues found. `create extension if not exists ltree;` (line 11) is the correct, idempotent way to provision the type before its use in the table definitions.
5. DDL correctness — idempotency: No issues found. `create extension if not exists`, `create schema if not exists`, and `create table if not exists` make `ensure_schema` safe to call repeatedly, matching its docstring contract. (Caveat under Notes: `if not exists` does not migrate an existing table to add a new column.)
6. DDL correctness — PK/FK design: No issues found. `collection_path ltree primary key` on `document` and the `(collection_path, sort_order)` composite PK on `document_content` correctly model the one-document-to-many-sections grain. The FK `references {schema_name}.{document_table} (collection_path) on delete cascade` is correct, and the child's PK has `collection_path` as its leading column, so the FK is index-backed and cascade deletes do not require a sequential scan — no separate FK index is needed.
7. DDL correctness — `source_binary_hash numeric(20,0)` sizing: No issues found. `numeric(20,0)` holds up to `10^20 - 1 = 99,999,999,999,999,999,999`, which exceeds `2^64 - 1 = 18,446,744,073,709,551,615` (both 20 digits). Precision 20 is sufficient for the full unsigned 64-bit range `[0, 2^64)`, and `bigint` (signed, max `~9.2e18`) would indeed overflow, so the `numeric` choice and its inline rationale are correct.
8. DDL correctness — column/nullability mapping to models: No issues found. `document` columns map to `cleaned_models.Document` (`n_parsed_sections`, `binary_hash` -> `source_binary_hash`) plus config-attached `collection_path`/`title` and server-default `ingested_at`. `document_content` columns map 1:1 to `cleaned_models.Section` (`sort_order`, `heading_text`, `content_text`, `word_count`, `page_start`, `page_end`); nullability matches (`heading_text`/`content_text`/`page_start`/`page_end` nullable; `sort_order`/`word_count` not null).
9. Placeholder/substitution soundness: No issues found. The only placeholders are `{schema_name}`, `{document_table}`, `{content_table}`, all substituted by `ensure_schema` with values passed through `validate_sql_identifier` (anchored `^[a-z_][a-z0-9_]*$`, `fullmatch`), and the post-render leftover-`{...}` guard fails loudly on any unsubstituted token. The DDL introduces no placeholder the substituter does not know about.

## Status & Next Steps

**Current Status**: Review complete. The DDL is correct and idempotent, the PK/FK design and cascade are sound, and `numeric(20,0)` is the right type for the uint64 hash. Findings are defense-in-depth CHECK constraints that would make the DB enforce the same invariants the Pydantic models enforce in the application layer.
**Completed**:
1. Read sql-development best-practices and the code-review template.
2. Reviewed schema.sql against best-practices and general DDL correctness.
3. Cross-checked column/type/nullability mapping against `cleaned_models.py` (`Document`, `Section`, `CleanedDocument`) and the substitution logic in `_utils.ensure_schema`.
4. Verified `numeric(20,0)` >= `2^64 - 1` arithmetically.
**Next Steps**:
1. Decide whether DB-level CHECKs (1.1-1.3) are wanted; they duplicate model validation but harden the DB against direct/buggy writes.
**Blockers**:
1. None.
**Notes**:
1. No [critical] or [major] findings. All findings are [suggestion]/[minor] — the file has no bugs, only optional integrity hardening.
2. `create table if not exists` is idempotent for first creation but does NOT add the newly-introduced `source_binary_hash` column to a pre-existing `document` table. If any environment already created `document` before this column was added, a separate `alter table ... add column` migration is required; `ensure_schema` will not perform it. Flagged as an operational note, not a finding in this file.
3. The CHECK suggestions assume the load step can write rows that bypass Pydantic validation. If every write path is guaranteed to go through `CleanedDocument`/`Section` validation first, the CHECKs are belt-and-suspenders; given the load step populates columns directly from the cleaned JSON, the DB CHECKs are a reasonable cheap safety net.
