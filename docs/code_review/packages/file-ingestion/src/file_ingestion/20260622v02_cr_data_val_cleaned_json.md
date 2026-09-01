---
name: cr-data_val_cleaned_json
goal: Confirm code quality of code/file_ingestion/data_validation/data_val_cleaned_json.py against python-development core skills after the v01 docstring update for the new `document` envelope + `binary_hash`.
created: 2026-06-22 14:10:00
updated: 2026-06-22 14:10:00
---

## Summary

This is a confirmation pass. Since v01 (`20260622v01_cr_data_val_cleaned_json.md`) only the module docstring changed — it now describes the new two-key envelope: a `document` block carrying `n_parsed_sections` plus the source `binary_hash` provenance, and the `sections` records. The validation logic is byte-for-byte the design v01 signed off on: it reads `read_bytes()` under `except OSError` and hands the bytes to `CleanedDocument.model_validate_json`, which folds non-UTF-8 decode, malformed JSON, and schema violations into one `except pydantic.ValidationError` → FAIL path. That fix from v01 is intact (lines 60-78). The updated docstring is accurate against `cleaned_models.py` and does NOT re-introduce the model/docstring drift v01 warned about: it summarises the checks and explicitly defers to `cleaned_models.CleanedDocument` as the authoritative rule list ("this module does not re-enumerate it to avoid drift", line 16) rather than restating each invariant. `binary_hash` is correctly described as Docling's `origin.binary_hash` and "non-negative", matching `Document.binary_hash: int = Field(ge=0)`; `n_parsed_sections` is correctly placed inside the `document` block, matching `Document.n_parsed_sections`. Config resolution, exit codes, naming, type hints, and logging are unchanged and correct. No critical or major findings. Severity counts: 0 critical, 0 major, 0 minor, 2 suggestions. Top item (suggestion): the docstring still leans toward enumerating individual checks (line 12-16) where a single "see the model" pointer would be more drift-proof, and one cross-field invariant it does name (`n_parsed_sections`) is more precisely "must equal `len(sections)`" than the looser "content metric" phrasing on line 13.

## Implementation Plan

1. [pending] Docstring precision/drift hygiene - `code/file_ingestion/data_validation/data_val_cleaned_json.py`
   - 1.1. [suggestion] Lines 12-16: The docstring partially re-enumerates the delegated checks ("the ``n_parsed_sections``/``sort_order``/``word_count``/page-range checks, the non-negative ``binary_hash``, and at least one section"). This is the exact pattern v01's finding 3 flagged as a drift risk; it is currently accurate against `cleaned_models.py`, but every named rule is one more thing to keep in sync with the model. The list already ends with "this module does not re-enumerate it to avoid drift" (line 16), which slightly undercuts the enumeration immediately preceding it. Consider trimming the named-rule list to the envelope shape only and letting the "see that model for the exact rule list" pointer carry the per-field detail. Low priority — the current text is correct, this is purely future-proofing.
   - 1.2. [suggestion] Line 13: `n_parsed_sections` is described as "the ``n_parsed_sections`` content metric". The model's actual invariant is stronger and more specific: `Document.n_parsed_sections` must equal `len(sections)` (enforced by `CleanedDocument._check_invariants`, `cleaned_models.py:161-165`) and carries `Field(ge=1)`. "Content metric" reads as a free-floating count and does not convey that it is a cross-checked equality. If the named rule is kept (see 1.1), consider "the ``n_parsed_sections`` count (cross-checked against ``len(sections)``)". Cosmetic.

## Skills with No Issues

1. Type Hints: No issues found - `validate_cleaned_file(json_path: Path) -> list[str]` and `main() -> None` fully annotated; `all_failures: list[str]` and `raw`/`document` locals use modern inferred/`list[...]` syntax. Unchanged since v01.
2. Docstrings (structure): No issues found - module docstring plus Google-style docstrings on both functions with Args/Returns. The updated module docstring accurately reflects the `document` envelope (`n_parsed_sections` + `binary_hash`) and `sections`; the only notes are the cosmetic phrasing/drift items in 1.1-1.2, not missing or malformed sections.
3. Comments: No issues found - comments explain the "why": lines 30-32 justify importing the schema from the package root (avoiding the heavy `docling_core` dependency); lines 65-68 explain the single `except` covering decode, parse, and shape and why bytes are read instead of text; lines 73-74 explain the `<document>` loc fallback; lines 130-131 explain the directory-level diagnostic. All accurate against the current code.
4. Logging: No issues found - uses `logconfig` (`setup_logging`/`get_logger`), no `print()`, f-strings throughout, `"=" * 60` run separators at start/end, log dir mirrors script location (`logs/file_ingestion/data_validation`), PASS logged with section count.
5. Exception Handling: No issues found - config read catches `(tomllib.TOMLDecodeError, OSError)` (line 113), config-field access catches `KeyError` (line 122), the read path catches `OSError` (line 62), and the schema check catches `pydantic.ValidationError` (line 71). All specific, all with context. The v01 read-path defect (`UnicodeDecodeError` escaping) is resolved: bytes are passed to `model_validate_json`, which surfaces a decode failure as a `ValidationError`, so a non-UTF-8 file becomes a FAIL message rather than a run-crashing traceback.
6. Executable Scripts: No issues found - `main()` with `if __name__ == "__main__"`, single required `--config` argument, logging deferred until after argparse, `sys.exit(1)` on every failure path (missing config, TOML decode/OS error, missing `[clean].cleaned_dir` / `[module].documents`, empty `stems`, missing `cleaned_dir`, any per-file failure) and clean exit on success.
7. Data Validation: No issues found - correctly named `data_val_` and located under `data_validation/` alongside the validated `cleaned_models`/clean-step output; delegating wholly to the shared Pydantic model is the single-source-of-truth design the data-validation skill favours.
8. Unit Tests: N/A - target is the validator script itself; the test file (`test_data_val_cleaned_json.py`) is reviewed separately.
9. SQL: N/A - this file contains no SQL.

## Status & Next Steps

**Current Status**: Confirmation pass complete. The v01 docstring-drift and read-path findings remain resolved; the new `document`-envelope docstring is accurate against `cleaned_models.py`. No critical/major/minor findings; two cosmetic suggestions.
**Completed**:
1. Read the current file in full plus `cleaned_models.py` (`Document`, `Section`, `CleanedDocument`) to verify the docstring claims.
2. Confirmed the docstring's envelope description matches the model: `n_parsed_sections` lives in `Document` (line 120), `binary_hash` is `Field(ge=0)` and documented as `origin.binary_hash` provenance (lines 110-115, 121), and `sections: list[Section]` is the second top-level key (line 144). No re-introduced drift.
3. Confirmed the v01 read-bytes/UTF-8 fix is intact: `read_bytes()` under `except OSError` (lines 60-63) with bytes passed to `model_validate_json` and the single `except pydantic.ValidationError` (lines 69-78) covering decode, parse, and shape.
4. Re-verified config resolution (`[clean].cleaned_dir`, `[module].documents`, `stems`), the empty-`stems` and missing-`cleaned_dir` early diagnostics, and the exit-code paths — all unchanged and correct.
**Next Steps**:
1. Optionally trim the named-rule list in the module docstring toward a pure "see the model" pointer (1.1) and tighten the `n_parsed_sections` phrasing (1.2). Both cosmetic; no functional change.
**Blockers**:
1. None.
**Notes**:
1. The delegate-to-model design continues to mean the validator cannot see anything the model cannot — the only such gap (the read/decode path) is already covered by reading bytes and letting Pydantic report the decode failure, so there is no new structural caveat introduced by the envelope change.
2. v01's intentionally-deferred PASS/FAIL logging asymmetry (PASS inside the helper, FAIL collected in `main()`) is unchanged and remains harmless; not re-raised here.
