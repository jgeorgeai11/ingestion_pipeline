---
name: cr-cleaned_models
goal: Address code quality observations identified in code/file_ingestion/cleaned_models.py to align with python-development skills.
created: 2026-06-22 00:00:00
updated: 2026-06-22 00:00:00
---

## Summary

`cleaned_models.py` is a small, high-quality Pydantic v2 schema module: the single source of truth for the cleaned-document JSON shared by the producer (`docling_section_parser.sections_to_record`) and the file validator (`data_validation.data_val_cleaned_json`). Since v01 the schema was restructured from a flat shape into a two-key envelope — a new `Document` sub-model holds `n_parsed_sections` (moved down from the top level) and `binary_hash`, and `CleanedDocument` is now `{document: Document, sections: list[Section]}` with the count/contiguity/non-empty invariants moved to a `CleanedDocument` model_validator that references `document.n_parsed_sections`. The restructure is correct and well-documented: all three models are `strict=True, extra="forbid"`, the load-bearing field order (`document` before `sections`; `n_parsed_sections` before `binary_hash`) matches the producer's documented envelope and stable on-disk key order, and the pydantic-only dependency boundary is preserved. The v01-resolved findings (page `ge=1`, no-empty-section guard, word_count semantics on `Section`) are all intact. The one substantive finding is an interaction introduced by the restructure: the `has zero sections` branch is now unreachable because v01's `n_parsed_sections` `ge=1` plus the count-equality-first ordering forecloses the only path that could reach it. The remainder are optional suggestions.

**Counts by severity:** critical: 0, major: 0, minor: 1, suggestion: 2.

**Top issue (minor):** the `if not self.sections: raise ValueError("has zero sections")` branch (lines 166-167) is dead — `n_parsed_sections: int = Field(ge=1)` (line 120) plus the count-equality check running first (lines 161-165) means the zero-section path is rejected before this branch can ever fire, so the producer's "most likely" empty-document failure surfaces as a field error or count-mismatch message, never "has zero sections".

## Implementation Plan

1. [completed] Unreachable invariant branch (restructure interaction) - `code/file_ingestion/cleaned_models.py`
   - 1.1. [completed] RESOLVED 2026-06-22: Reordered `CleanedDocument._check_invariants` so the non-empty `sections` check runs BEFORE the count-equality check; a `n_parsed_sections>=1, sections=[]` document now raises the explicit "has zero sections" (the branch is reachable) and short-circuits before the count arithmetic; updated `test_zero_sections_rejected` in `test_cleaned_models.py` to `pytest.raises(ValidationError, match="zero sections")` and repointed `test_validate_cleaned_file_empty_sections_list_caught` in `test_data_val_cleaned_json.py` to assert `"has zero sections"`; full suite green (186 passed).
   - 1.1. [minor, completed] Lines 166-167: The `if not self.sections: raise ValueError("has zero sections")` check can never fire as ordered. The count-equality check (lines 161-165) runs first; for `not self.sections` (i.e. `len(sections) == 0`) to be reached, the count check must have passed, which requires `document.n_parsed_sections == 0`. But `Document.n_parsed_sections` is `Field(ge=1)` (line 120), so a `Document` with `n_parsed_sections == 0` is rejected at the field level before `CleanedDocument._check_invariants` ever runs. Concretely: the producer building an empty document calls `Document(n_parsed_sections=0, ...)` (parser line 348), which fails on `ge=1`; the validator path with on-disk `{"document": {"n_parsed_sections": 5, ...}, "sections": []}` fails the count-mismatch branch (lines 162-165), not this one. So the empty-document case the producer docstring calls the most likely failure (parser lines 344-345) never yields the "has zero sections" message. This is a v01/restructure interaction: v01 tightened `n_parsed_sections` to `ge=1` when it lived at the top level; the restructure kept that bound and placed the count check first, which together orphan this branch. Reorder so the non-empty check precedes the count check (making the clearer message reachable and short-circuiting before the count arithmetic), or document it as deliberate defense-in-depth.
        - Current:
          ```python
          if self.document.n_parsed_sections != len(self.sections):
              raise ValueError(
                  f"n_parsed_sections ({self.document.n_parsed_sections}) != "
                  f"len(sections) ({len(self.sections)})"
              )
          if not self.sections:
              raise ValueError("has zero sections")
          ```
        - Expected:
          ```python
          if not self.sections:
              raise ValueError("has zero sections")
          if self.document.n_parsed_sections != len(self.sections):
              raise ValueError(
                  f"n_parsed_sections ({self.document.n_parsed_sections}) != "
                  f"len(sections) ({len(self.sections)})"
              )
          ```

2. [pending] Constraint coverage on the new Document sub-model - `code/file_ingestion/cleaned_models.py`
   - 2.1. [suggestion] Line 121: `binary_hash: int = Field(ge=0)`. `int` is the correct type and `ge=0` is the correct floor — Python ints are arbitrary-precision and the stdlib `json` module round-trips a full uint64 literal losslessly, so `model_dump()` -> JSON -> `model_validate_json` is exact even for values whose top half exceeds a signed bigint (the case the docstring at lines 110-115 motivates). Do NOT tighten to `ge=1`: Docling's `origin.binary_hash` can legitimately be `0`. The only optional hardening is an explicit upper bound asserting the unsigned-64-bit ceiling, mirroring v01's page-bound rationale of catching a corrupted out-of-range value in a persisted file. (The cross-language precision caveat — JS `Number` / some DB drivers losing precision above 2^53 — applies only to non-Python consumers and is the load step's concern, out of this file's scope.)
        - Current: `binary_hash: int = Field(ge=0)`
        - Expected: `binary_hash: int = Field(ge=0, le=18446744073709551615)`

3. [pending] Naming - `code/file_ingestion/cleaned_models.py`
   - 3.1. [suggestion] Line 96: `Document` is a generic name and the producer imports both this `Document` (parser line 54) and Docling's document module (parser line 47). There is no actual collision today — Docling's class is `DoclingDocument`, not `Document` — and the name reads well as the `document` envelope key it maps to. Noting only for awareness; a more specific name such as `DocumentMeta` or `DocumentInfo` would remove any future ambiguity. No change required.

## Skills with No Issues

1. Type Hints: No issues found — `Self` return on both validators, `str | None` modern union syntax, `Annotated[int, Field(ge=1)] | None` on page bounds, `list[Section]` and `Document` field types; all functions annotated.
2. Docstrings: No issues found — module, all three classes, and both validators have Google-style docstrings with Attributes/Returns/Raises. The new `Document` docstring (lines 97-116) documents the envelope role, the `binary_hash` uint64/`ge=0` rationale, and that `n_parsed_sections` equality is enforced at the `CleanedDocument` level; the `CleanedDocument` docstring (lines 125-139) documents the two-key envelope and the count invariant spanning it. The word_count semantics text (lines 40-44) carrying the v01 fix ("0 when neither contributes any words: both absent, empty, or whitespace-only") is intact and accurate.
3. Comments: No issues found — the module docstring's "field declaration order is load-bearing" note (lines 13-16) explains the why (model_dump key order = on-disk JSON order); both sub-models repeat the on-disk-order note where the order matters.
4. Exception Handling: No issues found — invariants raise `ValueError` with offending values embedded (count mismatch, sort_order, word_count, page range); Pydantic aggregates into `ValidationError` for callers. No bare excepts (none present). The dead branch at 1.1 is a reachability issue, not an exception-handling defect.
5. Logging: N/A — pure declarative schema with no runtime/I/O logic.
6. Executable Scripts: N/A — importable module, no CLI entry point.
7. Data Validation: N/A as a script — this is the schema the `data_val_` script consumes, not a `data_val_` script itself.
8. Unit Tests: N/A to this file — a dedicated `unit_tests/test_cleaned_models.py` was added under v01 finding 3.1; this review did not re-audit it (scope is the single source file). The restructure does change the envelope shape those tests construct, so confirm they were updated to the `{document: Document(...), sections: [...]}` form; that is a test-file concern outside this review's scope.
9. SQL / dbt: N/A — no SQL.

## Status & Next Steps

**Current Status**: Review complete; findings pending implementation. No code modified.
**Completed**:
1. Read code-review and python-development skills in full.
2. Read the v01 review (`20260622v01_cr_cleaned_models.md`) for context.
3. Reviewed the current whole `cleaned_models.py` against all relevant skills.
4. Cross-read both consumers (`docling_section_parser.py` producer path lines 324-351, `data_val_cleaned_json.py`) to verify the envelope field order, the pydantic-only boundary, and the binary_hash flow.
**Next Steps**:
1. Decide on 1.1 (reorder the non-empty check ahead of the count check, or annotate it as intentional defense-in-depth).
2. Optionally apply 2.1 (uint64 upper bound) and consider 3.1 (naming).
**Blockers**:
1. None.
**Notes**:
1. Restructure correctness confirmed: the count invariant now spans the envelope (`document.n_parsed_sections == len(sections)`, line 161), 1-based contiguity uses exact list equality against `range(1, N+1)` (lines 168-170, enforcing sorted AND 1-based), and `Section` is unchanged with its v01 invariants intact (page `ge=1` lines 57-58; both-None guard lines 75-76; word_count equality lines 77-84).
2. `strict=True, extra="forbid"` present at all three model levels (lines 51, 118, 141). `extra="forbid"` is consistent with `collection_path`/`title` being attached later by the load step (docstrings lines 102-103, 130-131): the on-disk file validated here legitimately omits them, and forbidding extras catches stray keys without conflicting with that deferred-identity design.
3. Field order is load-bearing and correct: `document` before `sections` and `n_parsed_sections` before `binary_hash` match the producer's documented dict envelope `{"document": {"n_parsed_sections", "binary_hash"}, "sections": [...]}` (parser lines 334-335), so `model_dump()` yields stable on-disk JSON key order.
4. Dependency boundary intact: imports `pydantic` and `typing` (`Annotated`, `Self`) only — no `docling_core` — so the validator depends on the schema without the heavy parse-time dependency (module docstring lines 10-11).
5. The v01 `has zero sections` message was reachable when `n_parsed_sections` lived at the top level and the count check could be reordered around it; the restructure plus `ge=1` is what orphaned it (finding 1.1). This is precisely the kind of regression a flat-schema review (v01) could not have anticipated.
