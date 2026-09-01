---
name: cr-data_val_embeddings
goal: Address code quality issues identified in code/embedding_generation/data_validation/data_val_embeddings.py to align with python-development and sql-development skills and verify the output validator correctly proves the embedding table's invariants.
created: 2026-06-25
updated: 2026-06-25
---

## Implementation Plan

1. [completed] Type Hints - `code/embedding_generation/data_validation/data_val_embeddings.py`
   - 1.1. [major] Line 90: The `count_tokens` parameter of `validate_embeddings` has no type annotation, while every other parameter in the signature is annotated. The type-hints skill requires type hints on all parameters. The callable is produced by `make_token_counter`, whose declared return type is `Callable[[str], int]`.
        - Current: `        count_tokens,`
        - Expected: `        count_tokens: Callable[[str], int],`
        - Note: requires adding `from collections.abc import Callable` to the imports (currently absent — confirmed via grep, only `argparse, logging, os, sys, tomllib, pathlib, dotenv, sqlalchemy` are imported). This mirrors the import already present in `_utils.py` and `chunker.py`.

2. [completed] Database Connection Correctness - `code/embedding_generation/data_validation/data_val_embeddings.py`
   - 2.1. [major] Line 78: `_get_engine` builds the connection string by interpolating raw credentials into an f-string DSN. A password (or user) containing reserved URL characters (`@ : / # ? %`) will mis-parse or break connection. The collaborating `generate_embeddings._get_engine` (lines 100-108) deliberately uses `sqlalchemy.URL.create(...)`, which percent-escapes credentials. This validator diverges from that convention in a module described as recently aligned to conventions, and the divergence is a latent connection bug, not just style.
        - Current: `    return create_engine(f"postgresql://{user}:{password}@{host}:{port}/{db_name}")`
        - Expected: build the URL with `URL.create("postgresql", username=user, password=password, host=host, port=int(port), database=db_name)` and pass it to `create_engine`; add `URL` to the `from sqlalchemy import ...` line (currently `create_engine, inspect, text`).
        - Rationale: matches the sibling generator exactly, so both scripts connect identically, and removes the credential-escaping bug.

3. [completed] Validation Completeness (chunk_tsv generated-column guarantee) - `code/embedding_generation/data_validation/data_val_embeddings.py`
   - 3.1. [minor] Lines 206-217: Check #4 confirms a column named `chunk_tsv` exists via `information_schema.columns`, but does not confirm it is a GENERATED column. The table guarantee under review is specifically the `chunk_tsv tsvector generated always as (...) stored` column (generate_embeddings.py line 290). A plain, manually-populated `tsvector` column named `chunk_tsv` would pass this check yet violate the guarantee (it could drift from `chunk_text`).
        - Current: query selects `count(*) ... where ... column_name = 'chunk_tsv'`
        - Expected: also assert the column is generated, e.g. add `and is_generated = 'ALWAYS'` to the `information_schema.columns` predicate (or check `generation_expression is not null` via `pg_attribute`/`pg_attrdef`).
        - Rationale: proves the FTS column is automatically kept in sync with `chunk_text`, which is the actual invariant the storage relies on. The companion GIN-index check (lines 219-230) already pairs with this; tightening the column check closes the gap.

## Skills with No Issues

1. data-validation: No issues. Script is correctly named `data_val_embeddings.py` and placed under `code/embedding_generation/data_validation/`, validating its sibling `generate_embeddings.py`, exactly per the skill's file-organization convention. Checks are output-at-rest assertions with per-check failure messages.
2. executable-scripts: No issues. `main()` with `if __name__ == "__main__": main()`; single required `--config` argparse argument; logging deferred until after argparse (lines 309-316) so `--help` creates no log files; config existence checked before `tomllib.load`.
3. logging: No issues. Uses `logconfig.get_logger`/`setup_logging`, never `print`; log dir mirrors script location (`logs/embedding_generation/data_validation`); f-strings throughout; `"=" * 60` run-boundary separators in `main`; single-word-over-budget case logged at WARNING, hard failures at ERROR — appropriate levels.
4. exception-handling: No issues. No bare excepts; catches are specific (`KeyError`, `tomllib.TOMLDecodeError`/`OSError`, `ValueError`, `SQLAlchemyError`); `_get_engine` chains with `raise ... from e`; messages include context (missing env var name, table name, model name).
5. type-hints: One issue (finding 1.1). All other functions and parameters use modern syntax (`list[str]`, `dict | None`, `-> None`).
6. docstrings: No issues. Module docstring enumerates the invariants; all functions have Google-style docstrings with Args/Returns/Raises; the `max_tokens` vs `max_seq_length` distinction and the single-word edge case are documented in the `validate_embeddings` docstring.
7. comments: No issues. Numbered inline comments explain the "why" of each check (e.g. truncation gate vs budget gate, why orphans are checked independently of the FK, why the single-word case is a warning not a failure).
8. sql best-practices (best-practices.md): No issues. Lowercase keywords; values parameterized via bind params (`:dim`, `:schema`, `:tbl`, and `_build_source_filter_clause`'s `sf_*` params); identifiers validated through `validate_sql_identifier` before interpolation (lines 124-128); aliases qualified in joins; NULL handled explicitly (`embedding is null`). Queries are short and direct; CTEs are not warranted at this complexity.

### Correctness verification (the invariants this validator must prove)

The following were checked against the source and the collaborators and are correct — recorded so the next reviewer need not re-derive them:

1. Unembedded-rows check (lines 149-170): `not exists` join on the PK columns, scoped by `source_filter` via the reused `_build_source_filter_clause`. The `n_src` denominator is scoped by the same filter and params, so the ratio is meaningful. Correct.
2. `source_filter` ltree handling: the validator reuses the generator's `_build_source_filter_clause`, which applies the `::text` cast (generate_embeddings.py line 379) so LIKE works on the `ltree` `collection_path` identity. Scoping matches generation exactly. Correct.
3. Orphan check (lines 192-201) is intentionally NOT scoped by `source_filter` — orphans are absolute FK violations regardless of the generation filter. Correct.
4. Token gates (lines 245-294) tokenize stored `chunk_text` with the model's OWN tokenizer via `make_token_counter` (the chunker's convention), and split into: (a) hard-FAIL truncation gate against `max_seq_length`, and (b) budget gate against `max_tokens` that downgrades single-word over-budget chunks (`word_count <= 1`) to a WARNING — matching the chunker's documented unsplittable-word behavior and the header `word_count` semantics in generate_embeddings.py (line 577, word_count stays the body count). Correct.
5. The truncation-safety gate (`over_seq_length`) is computed independently of `word_count`, so the single-vs-multi-word distinction only affects the non-safety budget gate. The truncation invariant therefore holds even in the pathological header-flooring edge (generate_embeddings.py lines 561-567), which is correctly a benign budget-gate concern, not a safety hole.
6. Dimension/null check (lines 172-188) uses `vector_dims(embedding) <> :dim or embedding is null`; `chunk_tsv` GIN index check (lines 219-230) matches `indexdef ilike '%using gin%chunk_tsv%'`. Both correct (the column-is-generated gap is finding 3.1).

## Status & Next Steps

**Current Status**: First per-file review complete. The validator is structurally sound and its invariant logic is correct; three improvements identified (two major, one minor). No critical issues.

**Completed**:
1. Read all python-development core sub-docs (type-hints, docstrings, comments, logging, exception-handling, executable-scripts, data-validation) and sql best-practices.
2. Read the validator, `generate_embeddings.py`, `_utils.py` (`make_token_counter`), and `chunker.py`.
3. Verified each invariant the embedding table guarantees against the validator's SQL and against the generator's behavior.
4. Confirmed via grep that `Callable` and `URL` are not currently imported.

**Next Steps**:
1. Add the `count_tokens: Callable[[str], int]` annotation and `from collections.abc import Callable` import (finding 1.1).
2. Switch `_get_engine` to `URL.create` for credential-safe DSN construction, matching the generator (finding 2.1).
3. Tighten the `chunk_tsv` check to assert the column is GENERATED (finding 3.1).

**Blockers**:
1. None.

**Notes**:
1. Review-only; no source files were modified and nothing was committed.
2. The validator correctly mirrors the generator's config defaults (`max_tokens` default 500) and PK auto-detection, so its scope matches what was generated.
