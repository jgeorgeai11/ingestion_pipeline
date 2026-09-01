---
name: cr-cleaned_models
goal: Address code quality observations identified in code/file_ingestion/cleaned_models.py to align with python-development skills.
created: 2026-06-22 00:00:00
updated: 2026-06-22 00:00:00
---

## Summary

`cleaned_models.py` is a small, high-quality Pydantic v2 schema module that serves as the single source of truth for the cleaned-sections JSON, shared by the producer (`docling_section_parser.sections_to_record`) and the file validator (`data_validation.data_val_cleaned_json`). Both models are `strict=True, extra="forbid"` with `@model_validator(mode="after")` invariants. Type hints and Google-style docstrings are complete and compliant, error messages embed the offending values (per the exception-handling skill), and the load-bearing field-order rationale is documented. The invariants are correct as written. Findings are limited to one minor docstring inaccuracy and several optional constraint-tightening suggestions; there are no critical or major issues.

**Counts by severity:** critical: 0, major: 0, minor: 2, suggestion: 4.

**Top issue (minor):** the `word_count` docstring (lines 37-38) claims `0 only when both are absent`, but `0` also occurs for empty/whitespace-only strings since `"".split() == []`.

## Implementation Plan

1. [completed] Docstring precision - `code/file_ingestion/cleaned_models.py`
   - 1.1. [minor] Lines 37-38: The parenthetical "0 only when both are absent" is inaccurate. `word_count` is also `0` when `heading_text`/`content_text` are present but empty or whitespace-only, because `"".split()` and `"   ".split()` both return `[]`. The invariant compares against `(text or "").split()`, so an empty string and `None` are indistinguishable for the count.
        - Current: `0 only when both are absent`
        - Expected: `0 when neither contributes any words (both absent, empty, or whitespace-only)`
        - **RESOLVED (1.1):** Updated the `word_count` Attributes docstring to read "0 when neither contributes any words: both absent, empty, or whitespace-only, since `"".split()` and `"   ".split()` both return `[]`".
   - 1.2. [minor] Line 104: `n_parsed_sections: int = Field(ge=0)`. Given the document-level invariants (non-empty `sections` at line 124 plus the equality check at line 119), the smallest reachable value is `1`, so the `ge=0` bound can never actually reject input — it is dead relative to the stricter invariants. Either tighten to `ge=1` to express the true floor declaratively, or drop the field constraint and rely on the model validator. (`word_count = Field(ge=0)` at line 50 is genuinely useful and should stay, since a single Section in isolation may legitimately have `word_count == 0`.)
        - Current: `n_parsed_sections: int = Field(ge=0)`
        - Expected: `n_parsed_sections: int = Field(ge=1)`
        - **RESOLVED (1.2):** Tightened `n_parsed_sections` to `Field(ge=1)`; left `word_count = Field(ge=0)` unchanged as a lone Section may legitimately have `word_count == 0`.

2. [completed] Constraint coverage / under-constrained fields - `code/file_ingestion/cleaned_models.py`
   - 2.1. [suggestion] Lines 48-49: There is no invariant requiring at least one of `heading_text` / `content_text` to be non-null. `Section(sort_order=1, heading_text=None, content_text=None, word_count=0, page_start=None, page_end=None)` validates cleanly. The producer never emits such a section — `_SectionAccumulator.is_empty()` (parser line 106-108) drops heading-and-content-less sections before `build()` — so the schema is looser than the producer's guarantee. Since the schema is the single source of truth that the validator enforces against arbitrary on-disk files, consider adding a Section-level check that rejects fully-empty sections, e.g. `if self.heading_text is None and self.content_text is None: raise ValueError("section has neither heading nor content")`.
        - **RESOLVED (2.1):** Added a `heading_text is None and content_text is None` guard at the top of `Section._check_invariants` raising `ValueError("section has neither heading nor content")`, and documented the "must carry a heading or content" guarantee in the class docstring; only the both-None case is rejected (empty strings are still accepted) to avoid over-tightening.
   - 2.2. [suggestion] Lines 51-52: `page_start` / `page_end` have no lower bound. Docling provenance `page_no` values are 1-based, so a `Field(ge=1)` (applied via `Annotated[int, Field(ge=1)] | None`) would catch a corrupted `0` or negative page in a persisted file. The cross-field `page_start <= page_end` check (lines 77-84) is correct but does not constrain the absolute range.
        - **RESOLVED (2.2):** APPLIED after a clean safety check — both `page_start` and `page_end` are now `Annotated[int, Field(ge=1)] | None`. The safety check found 0 sections with `page_start`/`page_end` < 1 across the 7 real cleaned JSON files and 0 rows in `cms_iom.document_content`, so tightening rejects no real persisted data; `| None` and the cross-field `page_start <= page_end` check are preserved.
   - 2.3. [suggestion] Line 47: `sort_order` carries no field-level bound. This is intentional and acceptable — the document-level contiguity invariant (lines 126-132) fully constrains it across the list, and a Section validated in isolation should not assume document position. Noting only so the absence reads as deliberate rather than an oversight; no change required unless Section is expected to be valid standalone, in which case `Field(ge=1)` would help.
        - **RESOLVED (2.3):** Left as-is intentionally per the review — the document-level contiguity invariant fully constrains `sort_order` across the list; no change made.

3. [completed] Test coverage - `code/file_ingestion/cleaned_models.py`
   - 3.1. [suggestion] No dedicated `unit_tests/test_cleaned_models.py` exists. The invariants are exercised indirectly through `test_docling_section_parser.py` and `test_data_val_cleaned_json.py`, but the schema is the single source of truth and warrants direct negative tests asserting each invariant rejects bad input: `word_count` mismatch, `page_start > page_end`, `n_parsed_sections != len(sections)`, zero sections, non-contiguous `sort_order`, and strict-mode rejection of `bool` where `int` is expected (e.g. `word_count=True`). Co-locating these keeps the contract pinned independently of the consumers.
        - **RESOLVED (3.1):** Added `code/file_ingestion/unit_tests/test_cleaned_models.py` with positive cases (valid Section, heading-only, content-only, null pages, valid CleanedDocument) and negative cases pinning each invariant: word_count mismatch, page_start > page_end, fully-empty section (2.1), page < 1 (2.2), negative word_count, bool-as-int for `word_count`/`sort_order` under strict mode, n_parsed_sections != len(sections), zero sections, non-contiguous sort_order, and n_parsed_sections < 1 (1.2). Full `code/file_ingestion/unit_tests/` suite passes (121 passed).

## Skills with No Issues

1. Type Hints: No issues found — `Self` return on validators, `str | None` / `int | None` modern union syntax, `list[Section]` generics; all functions annotated.
2. Docstrings: No structural issues — module, both classes, and both validators have Google-style docstrings with Attributes/Returns/Raises. One precision fix noted at 1.1.
3. Comments: No issues found — the module docstring's "field declaration order is load-bearing" note explains the *why* (model_dump key order = on-disk JSON order), per the comments skill.
4. Exception Handling: No issues found — invariants raise `ValueError` with the offending values embedded in the message; Pydantic aggregates these into `ValidationError` for callers. No bare excepts (none present).
5. Logging: N/A — pure declarative schema with no runtime/I/O logic; logging would be inappropriate here.
6. Executable Scripts: N/A — importable module, no CLI entry point.
7. Data Validation: N/A as a script — this is the schema the `data_val_` script consumes, not a `data_val_` script itself.
8. Unit Tests: Issue noted at 3.1 (no dedicated test module for the schema).
9. SQL / dbt: N/A — no SQL.

## Status & Next Steps

**Current Status**: Review complete; all findings implemented and verified.
**Completed**:
1. Read code-review and python-development skills in full.
2. Reviewed `cleaned_models.py` against all relevant skills.
3. Cross-read both consumers (`docling_section_parser.py`, `data_val_cleaned_json.py`) to assess fit; verified line numbers against source.
4. Applied 1.1 (docstring precision) and 1.2 (`n_parsed_sections` -> `ge=1`).
5. Applied 2.1 (reject fully-empty Section) and, after a clean safety check, 2.2 (page bounds -> `Annotated[int, Field(ge=1)] | None`); 2.3 left as-is per the review.
6. Added `code/file_ingestion/unit_tests/test_cleaned_models.py` (3.1) with direct positive and negative tests for every invariant.
7. Adjusted one consumer fixture: `test_data_val_cleaned_json.py`'s former `test_validate_cleaned_file_null_text_fields_pass` (which built a both-None section now rejected by 2.1) was reframed as `test_validate_cleaned_file_null_content_text_passes`, a heading-only section that remains valid.
8. Verified: full `code/file_ingestion/unit_tests/` suite passes (121 passed); `data_val_cleaned_json.py` against the real cms_iom cleaned files exits 0 (PASS) under the tightened schema.
**Next Steps**:
1. None — implementation complete; changes are uncommitted and left for review.
**Blockers**:
1. None.
**Notes**:
1. Invariant correctness confirmed: word_count equality, `page_start <= page_end` (null-guarded), `n_parsed_sections == len(sections)`, non-empty, and 1-based contiguous `sort_order` are all implemented correctly.
2. "Validated not computed" rationale is sound and confirmed against the producer: the parser computes `word_count` with the identical `.split()` logic (parser line 123), so the equality invariant can never fail on the *producer* path — its real value is catching stale/corrupted persisted files on the *validator* path, exactly as the docstring (lines 36-38, 57-59) claims.
3. Strict-mode benefit confirmed: `strict=True` makes `bool` distinct from `int`, so `word_count=True` / `sort_order=False` are rejected rather than silently coerced.
4. Dependency boundary is correct: imports `pydantic` and `typing.Self` only — no `docling_core` — so the validator depends on the schema without pulling in the heavy parse-time dependency. This is the design's key payoff and is well documented (module docstring lines 10-11).
5. Serves both consumers well: the producer constructs `CleanedDocument(...)` and calls `model_dump()` (parser lines 276-277), relying on declared field order for stable on-disk JSON key order; the validator calls `model_validate_json` and maps each `ValidationError` to a FAIL line (validator lines 67-75). The shared `mode="after"` invariants mean producer and validator enforce the identical contract from one definition. The only asymmetry worth flagging is 2.1: the producer drops empty sections that the schema would still accept.
