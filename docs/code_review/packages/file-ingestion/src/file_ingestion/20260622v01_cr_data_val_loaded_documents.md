---
name: cr-data_val_loaded_documents
goal: Address code quality and SQL-correctness issues identified in code/file_ingestion/data_validation/data_val_loaded_documents.py to align with python-development and sql-development skills.
created: 2026-06-22 00:00:00
updated: 2026-06-22 00:00:00
---

## Summary

This is a well-structured, defensively-written DB-level output validator. SQL-injection safety is handled correctly (all interpolated identifiers pass `validate_sql_identifier`; all values are bound parameters), the executable-scripts pattern is followed faithfully, and most invariant checks are SQL-correct. The most significant issues are completeness gaps relative to the producer-side Pydantic schema (`CleanedDocument`): the DB validator silently passes a document that loaded **zero** content rows, and never asserts the `>= 1 section` invariant that the clean step enforces. No critical bugs were found; the findings are 0 critical / 1 major / 6 minor / 3 suggestion. Top issue: the n_parsed_sections-vs-count check (lines 157-175) cannot distinguish a correctly-loaded document from one whose content rows never landed when `n_parsed_sections` is reported as 0, and there is no separate "every document has at least one content row" check, so a document with no content rows would pass. The normal producer flow cannot reach this state (the `CleanedDocument` schema guarantees `n_parsed_sections >= 1`), so it is reachable only via a loader bug or data-at-rest drift — which is exactly what a DB-level output validator exists to catch.

## Implementation Plan

1. [completed] Completeness vs producer-side schema invariants - `code/file_ingestion/data_validation/data_val_loaded_documents.py`
   - 1.1. [major] [completed] Lines 157-175 (and overall): No check that each document has at least one content row. `CleanedDocument` (cleaned_models.py) enforces `sections` is non-empty and `n_parsed_sections == len(sections)`, so a loaded document must have `>= 1` content row. Check 2 here only asserts `n_parsed_sections == count(content rows)`; a document with `n_parsed_sections = 0` and 0 content rows passes both check 2 and the contiguity check (the `group by` produces no group for a document with no content rows). The DB-side validator therefore does not assert the non-empty invariant the producer guarantees. Normal producer flow cannot reach this state (the schema guarantees `n_parsed_sections >= 1`), so it is reachable only via a loader bug or data-at-rest drift — precisely the failure mode this validator exists to catch, which is why it is still worth asserting.
        - Current: only `having d.n_parsed_sections <> count(c.sort_order)`
        - Expected: add a dedicated check, e.g. `select d.collection_path::text from {doc} d left join {content} c on c.collection_path = d.collection_path group by d.collection_path having count(c.sort_order) = 0` → FAIL "document has zero content rows", mirroring `CleanedDocument`'s "has zero sections" rule.
        - RESOLVED: Added check 2a (left join doc -> content grouped by collection_path, `having count(content_row.sort_order) = 0`) emitting `FAIL: document <cp> has zero content rows`; aggregated into `failures`. Proven non-vacuous via a rolled-back insert (a content-less doc was flagged, then rolled back).

2. [completed] SQL correctness / redundant predicates - `code/file_ingestion/data_validation/data_val_loaded_documents.py`
   - 2.1. [minor] [completed] Lines 205-207: In the contiguity check the predicate `count(*) <> count(distinct sort_order)` is dead. `sort_order` is part of the composite primary key `(collection_path, sort_order)` (schema.sql), so within a `collection_path` group `sort_order` is already unique and `count(*)` can never differ from `count(distinct sort_order)`. The duplicate-detection branch can never fire. It is harmless defense-in-depth, but if kept it should be commented as such; otherwise it reads as guarding against a case the schema makes impossible. `min(sort_order) <> 1 or max(sort_order) <> count(*)` alone fully detects non-contiguity given uniqueness and positivity.
        - Current: `or count(*) <> count(distinct sort_order)`
        - Expected: drop the term, or add a one-line comment noting it is redundant given the composite PK and kept only as defense against a schema change.
        - RESOLVED: Dropped the `or count(*) <> count(distinct sort_order)` term; the `having` now ends on `max(sort_order) <> count(*)`. The `n_distinct` select column and `distinct={n_distinct}` diagnostic are kept for the failure message.
   - 2.2. [minor] [completed] Lines 195-215: The contiguity check assumes `sort_order >= 1`. The `min(sort_order) <> 1` term catches a non-1 minimum, but the combination `min=1, max=count(*)` does not exclude a sequence like `{1, 1, 2}` were duplicates possible — this is fine here only because the PK guarantees uniqueness (see 2.1). The correctness of this check is entirely load-bearing on that PK; a comment stating the dependency would make the reasoning explicit (current comment at line 196 explains the min/max/count strategy but not why uniqueness is assumed).
        - RESOLVED: Added a comment above the contiguity check stating the min=1/max=count(*) test is correct because the composite PK (collection_path, sort_order) guarantees sort_order uniqueness within a document.
   - 2.3. [minor] [completed] Lines 161-189 (and 198-245): Join/group aliases are single letters (`d`, `c`). The sql-development best-practices skill states: "use descriptive aliases (no single-letter)." Although `d`/`c` are conventional and used consistently, this is an explicit rule in the loaded skill and a conventions deviation by the validator's own remit.
        - Current: `from {doc} d left join {content} c on c.collection_path = d.collection_path`
        - Expected: `from {doc} as doc_row left join {content} as content_row on content_row.collection_path = doc_row.collection_path` (or similar descriptive aliases), applied consistently across checks 2 and 3.
        - RESOLVED: Renamed `d`/`c` to `doc_row`/`content_row` across the count-match (2), zero-content (2a), and orphan (3) checks. The single-table contiguity (4), word_count (5), and page-range (6) checks have no join aliases and use bare column names, so were left unchanged.

3. [completed] Connection / engine lifecycle - `code/file_ingestion/data_validation/data_val_loaded_documents.py`
   - 3.1. [minor] [completed] Lines 75, 119, 307-311: The Engine is never disposed. `validate_loaded_documents` uses `with engine.connect()` (good — the connection is released), but the Engine itself (and its pool) is created in `main()` and never `engine.dispose()`d. For a short-lived script this is low-impact, but wrapping the engine in `try/finally: engine.dispose()` (or `with engine.connect()`/disposing in `main`) is cleaner and avoids leaving pooled connections open if the process is reused.
        - Expected: dispose the engine in a `finally` in `main()` after validation completes or fails.
        - RESOLVED: `main()` now binds the engine in its own try (ValueError-guarded), then wraps `validate_loaded_documents` in a try/finally whose `finally` calls `engine.dispose()`, so the pool is closed on success or failure.
   - 3.2. [suggestion] [completed] Line 119: All six checks run in a single read-only `engine.connect()` block but the connection is not marked read-only and no explicit transaction isolation is set. The checks span multiple statements; if another process is concurrently loading, the six queries can observe different snapshots (each statement gets its own snapshot under the default READ COMMITTED). For an output validator run after load this is acceptable, but a single `engine.connect().execution_options(...)` with a REPEATABLE READ transaction (`with engine.begin()`), or a note that the validator assumes no concurrent writes, would make the consistency guarantee explicit.
        - RESOLVED (documentation note, not a transaction restructure): Added a note to the module docstring and the `validate_loaded_documents` docstring that the checks run as multiple statements under default READ COMMITTED and the validator assumes no concurrent writes (runs after a load completes), so cross-statement snapshot differences are not a concern. No REPEATABLE READ / `engine.begin()` restructure was made.

4. [completed] Config resolution / exit-code logic - `code/file_ingestion/data_validation/data_val_loaded_documents.py`
   - 4.1. [minor] [completed] Lines 290-298: If `config["module"]["documents"]` is an empty list, `expected_collection_paths` is `[]`, and the script proceeds. Check 0 (line 122) still requires `count(*) > 0`, so a populated DB with an empty config list would PASS (no expected docs to miss, and the existing rows satisfy the cross-table invariants). The sibling validator `data_val_cleaned_json.py` (lines 124-126) explicitly errors on an empty document list (`"Config lists no documents to validate"` → exit 1). For consistency and to avoid a vacuously-passing validation, add the same empty-list guard here.
        - Current: no guard; proceeds with `expected_collection_paths = []`
        - Expected: after building the list, `if not expected_collection_paths: logger.error("Config lists no documents to validate"); sys.exit(1)`.
        - RESOLVED: After building `expected_collection_paths`, added `if not expected_collection_paths: logger.error("Config lists no documents to validate"); sys.exit(1)`, matching the sibling `data_val_cleaned_json.py`.
   - 4.2. [suggestion] [completed] Lines 284-298: A document entry missing the `collection_path` key raises `KeyError` inside the list comprehension and is caught by the `except KeyError` at line 296, but the resulting message (`"Missing required config field: 'collection_path'"`) does not say which document. A loop that names the offending entry (index or `file`) would aid debugging, matching the per-document specificity used elsewhere.
        - RESOLVED: Replaced the list comprehension with a loop that, when a document entry lacks `collection_path`, logs `Document missing required 'collection_path': <file or index N>` and exits 1, naming the offending entry.

5. [completed] Documentation accuracy - `code/file_ingestion/data_validation/data_val_loaded_documents.py`
   - 5.1. [minor] [completed] Lines 12-19 (module docstring "Checks" list) and lines 85-110 (`validate_loaded_documents` docstring): Neither documents check 0 ("at least one document; empty schema is a failure", lines 120-124) nor the per-expected-document presence check 0a (lines 127-146). The function docstring (lines 90-93) does describe the presence check prose, but the module-level "Checks (all SQL)" enumeration omits both the emptiness and presence checks, so the list undersells what the script does. Add the "schema not empty" and "every expected document present" items to the module docstring's check list.
        - RESOLVED: The module docstring "Checks" list now enumerates the schema-not-empty check (0), the per-expected-document presence check (0a), and the new zero-content-row check (1.1), alongside the existing invariants; the `validate_loaded_documents` docstring was updated to match.

## Skills with No Issues

1. SQL-injection safety (sql-development best-practices + identifier validation): No issues. Every interpolated identifier (`db_schema`, `document_table`, `content_table`) is validated via `validate_sql_identifier` (lines 111-113) before being composed into `doc`/`content`; the composed names are the only interpolated tokens. All user/data values (`expected_collection_paths`) are passed as bound parameters (`:cps`, line 138), and `= any(:cps)` correctly adapts the Python list to a Postgres array under psycopg2. No string-interpolated values reach SQL.
2. SQL best-practices (style, other than aliasing): No issues beyond finding 2.3. Queries are lowercase, use explicit `left join ... on`, explicit `group by`/`having` with column names (not positional), and explicit `::text` casts on the `ltree` column.
3. Type Hints: No issues. All functions have complete parameter and return annotations using modern syntax (`list[str]`, `-> None`).
4. Exception Handling: No issues. Specific exceptions are caught (`KeyError`, `tomllib.TOMLDecodeError`/`OSError`, `ValueError`, `SQLAlchemyError`); `_get_engine` chains with `raise ... from e` (line 73); no bare `except`; failures are logged before `sys.exit(1)`.
5. Executable Scripts: No issues. Single `--config` argument, `main()` + `if __name__ == "__main__"`, logging deferred until after argparse (lines 261-265), config-existence and TOML-parse guards present.
6. Logging: No issues. Uses the shared `logconfig` `setup_logging`/`get_logger`; consistent `logger.info`/`logger.error` usage; no `print`.
7. Data Validation (naming/organization): No issues. File is named `data_val_*` and located under `code/file_ingestion/data_validation/`, matching the required layout.
8. NULL handling in checks (sql-development): Mostly correct. Check 2's `count(c.sort_order)` is safe because `content_table.sort_order` is NOT NULL (schema.sql), so the left-join count is 0 (not 1) for content-less documents. Checks 5 and 6 handle NULLs explicitly (`word_count is null`, `page_start is not null and page_end is not null`). See finding 1.1 for the one remaining gap.

## Status & Next Steps

**Current Status**: Implementation complete. All findings (1, 2, 3, 4, 5) resolved and verified against the live cms_iom DB; no commit made.
**Completed**:
1. Read python-development (data-validation, executable-scripts, exception-handling) and sql-development (best-practices) skills and the code-review template/example.
2. Reviewed the target file against those skills plus the producer-side schema (cleaned_models.py), the DDL (sql/schema.sql), the shared validators (_utils.py), the loader (ingest.py load step), and the sibling validator (data_val_cleaned_json.py).
3. Verified SQL-injection safety, transaction handling, and per-check SQL correctness (including the `= any(:cps)` array binding under psycopg2 and the composite-PK basis for the contiguity check).
4. Implemented finding 1.1 (new zero-content-row check 2a), 2.1/2.2 (dropped dead predicate, added PK-uniqueness comment), 2.3 (descriptive `doc_row`/`content_row` aliases), 3.1 (engine disposal in finally), 3.2 (consistency-assumption docstring note, no restructure), 4.1 (empty-document-list guard), 4.2 (named offending document on missing collection_path), and 5.1 (docstring check-list updates).
5. Verified: live run PASSED (exit 0) against cms_iom (7 docs / 584 rows); the new check 2a proven non-vacuous via a rolled-back insert (a content-less doc was flagged, 7->8 docs, then rolled back to 7 with the DB untouched); `pytest code/file_ingestion/unit_tests/` = 148 passed.
**Next Steps**:
1. None — ready for re-review.
**Blockers**:
1. None.
**Notes**:
1. Code was modified per the task; no commit was made, per task instructions.
2. The six SQL checks were each independently verified for off-by-one and NULL-handling correctness; checks 3 (orphan anti-join), 4 (contiguity), 5 (word_count), and 6 (page range) are correct, and check 2's content-row count is safe given the NOT NULL `sort_order`.
3. The non-vacuousness proof used an explicit `conn.begin()`/`trans.rollback()` (not `with engine.begin()`, which would commit on normal exit) and was run from a throwaway script that was deleted afterward; no scripts or `.coverage` artifacts remain.
