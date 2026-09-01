---
name: cr-test_docling_section_parser
goal: Address coverage gaps and unit-test standard deviations in code/file_ingestion/unit_tests/test_docling_section_parser.py to align with python-development unit-tests sub-skill.
created: 2026-06-18 13:41:38
updated: 2026-06-18 13:41:38
---

## Summary

The test suite is well-structured and passes (15 tests, all green). `pytest --cov`
confirms **93% line coverage** of `docling_section_parser.py` (97 stmts, 7 missed):

```
Name                                            Stmts   Miss  Cover   Missing
code/file_ingestion/docling_section_parser.py      97      7    93%   213-220, 257
```

The gap is concentrated entirely in the **error/edge paths**. The happy-path
cleaning rules are thoroughly covered; what is missing is precisely the surface
the task flagged as most important: the `parse_docling_json` failure modes
(missing file, garbage JSON, non-Docling JSON), several whole-document edge
cases (empty / furniture-only / no-heading), and the heading+content
`word_count` arithmetic. No `pytest.raises` / `match=` assertions exist anywhere
in the file, so the entire error contract documented in the module docstring
(`Raises: OSError`, `ValueError`) is unverified.

## Implementation Plan

1. [pending] Cover the error/edge paths in `parse_docling_json` - `code/file_ingestion/unit_tests/test_docling_section_parser.py`
   - 1.1. [major] Missing test: missing-file path is uncovered (module lines 213-215, the `except OSError: ... raise`). The `Raises: OSError` contract is never asserted.
        - Current: no test invokes `parse_docling_json` on a nonexistent path.
        - Expected: add a test that passes a path under `tmp_path` that does not exist and asserts `pytest.raises(OSError)`:
          ```python
          def test_parse_docling_json_missing_file_raises(self, tmp_path: Path) -> None:
              """A missing file raises OSError (the read-failure contract)."""
              with pytest.raises(OSError):
                  parse_docling_json(tmp_path / "does_not_exist.json")
          ```
   - 1.2. [major] Missing test: garbage (non-JSON) input is uncovered (module lines 216-220, the `except Exception: ... raise ValueError`). Standard 5.2 requires `pytest.raises(..., match=...)`; no `match=` appears anywhere in the file.
        - Current: no test feeds malformed bytes to the parser.
        - Expected: write a file containing non-JSON text and assert the predictable failure type and message:
          ```python
          def test_parse_docling_json_garbage_raises_value_error(self, tmp_path: Path) -> None:
              """Malformed (non-JSON) input raises ValueError, not a raw parse error."""
              bad = tmp_path / "bad.json"
              bad.write_text("this is not json {{{")
              with pytest.raises(ValueError, match="Invalid Docling JSON"):
                  parse_docling_json(bad)
          ```
   - 1.3. [major] Missing test: valid-JSON-but-not-a-DoclingDocument is uncovered (same lines 216-220, distinct input class). This is the realistic corruption case (e.g. a JSON file from the wrong pipeline step).
        - Current: not exercised.
        - Expected: write syntactically valid JSON that fails Docling's pydantic validation and assert `pytest.raises(ValueError, match="Invalid Docling JSON")`:
          ```python
          def test_parse_docling_json_non_docling_raises_value_error(self, tmp_path: Path) -> None:
              """Valid JSON that is not a DoclingDocument raises ValueError."""
              bad = tmp_path / "wrong.json"
              bad.write_text(json.dumps({"foo": "bar"}))
              with pytest.raises(ValueError, match="Invalid Docling JSON"):
                  parse_docling_json(bad)
          ```
   - 1.4. [minor] Parametrize the three error-path tests (1.2, 1.3) over the malformed-input variants, per standard 5.1, since they share one Act/Assert shape:
        - Expected: `@pytest.mark.parametrize("payload", ["not json {{{", json.dumps({"foo": "bar"})], ...)` driving a single `pytest.raises(ValueError, match="Invalid Docling JSON")` test.

2. [pending] Cover whole-document edge cases - `code/file_ingestion/unit_tests/test_docling_section_parser.py`
   - 2.1. [major] Missing test: empty document returns `[]`. The module documents "Empty when the document has no retainable content" (lines 203-204) and `sections_to_record` over `[]` is tested only on a hand-built list, never on an actually-parsed empty document.
        - Current: no test parses a `DoclingDocument(name="t")` with no body items.
        - Expected:
          ```python
          def test_parse_docling_json_empty_document_returns_empty(self, tmp_path: Path) -> None:
              """A document with no body items yields no sections."""
              doc = DoclingDocument(name="t")
              assert _parse(doc, tmp_path) == []
          ```
   - 2.2. [major] Missing test: furniture-only document returns `[]`. `test_fully_empty_unit_pruned` mixes furniture with a real heading+content; it never asserts the pure furniture-only → empty-result case (the `_flush` pruning of the sole pre-heading builder).
        - Current: not asserted.
        - Expected: a document containing only `PAGE_HEADER` / `PAGE_FOOTER` text, asserting `_parse(...) == []`.
   - 2.3. [major] Missing test: no-heading-at-all document yields a single `heading_text=None` section. This is distinct from `test_pre_heading_content_gets_none_heading`, which has a later heading and produces two sections; the single-leading-section terminal case is not covered.
        - Current: not asserted.
        - Expected:
          ```python
          def test_parse_docling_json_no_heading_single_leading_section(self, tmp_path: Path) -> None:
              """Content with no heading at all becomes one heading_text=None section."""
              doc = DoclingDocument(name="t")
              doc.add_text(label=DocItemLabel.TEXT, text="Only body.", prov=_prov(1))
              sections = _parse(doc, tmp_path)
              assert len(sections) == 1
              assert sections[0].heading_text is None
              assert sections[0].content_text == "Only body."
          ```

3. [pending] Cover the heading+content word_count arithmetic - `code/file_ingestion/unit_tests/test_docling_section_parser.py`
   - 3.1. [major] Missing test: `word_count` as the sum of heading + content words (module line 143, `len(heading.split()) + len(content.split())`). The two existing `word_count` asserts (`test_all_blank_table_skipped`, `test_heading_only_section_kept`) are both **heading-only** with `content_text=None`, so the `+` operand for content is always `0` — the summation behavior the docstring promises is never exercised.
        - Current: only heading-only word_count is asserted.
        - Expected: a section with both a multi-word heading and multi-word content, asserting the combined count:
          ```python
          def test_word_count_sums_heading_and_content(self, tmp_path: Path) -> None:
              """word_count is heading words plus content words."""
              doc = DoclingDocument(name="t")
              doc.add_heading(text="Two Words", level=1, prov=_prov(1))
              doc.add_text(label=DocItemLabel.TEXT, text="Three content words.", prov=_prov(1))
              sections = _parse(doc, tmp_path)
              assert sections[0].word_count == 5  # 2 heading + 3 content
          ```

4. [pending] Cover remaining branch and label-coverage gaps - `code/file_ingestion/unit_tests/test_docling_section_parser.py`
   - 4.1. [minor] Missing test: blank-heading element is uncovered (module line 257, the `if not heading: continue` guard — confirmed missing by `--cov`). A `section_header`/`title` whose text is whitespace should be skipped rather than opening a section.
        - Current: not exercised.
        - Expected: add a heading element with whitespace-only text and assert it does not produce a section.
   - 4.2. [suggestion] `section_header` boundary is covered only indirectly via `add_heading` (which emits `SECTION_HEADER`); `title` is covered explicitly by `test_title_element_becomes_section`. Coverage is adequate, but consider an explicit `DocItemLabel.SECTION_HEADER` test for symmetry with the title test so the two members of `HEADING_LABELS` are documented in parallel.

5. [pending] Naming convention alignment - `code/file_ingestion/unit_tests/test_docling_section_parser.py`
   - 5.1. [minor] Lines 86-263, 269-312: test functions follow `test_<scenario>_<expected>` and omit the `<function>` segment required by standard 2.2 (`test_<function>_<scenario>_<expected>`). The class grouping (`TestCleaningRules`, `TestSectionsToRecord`) partially conveys the function under test, which mitigates this, but the names do not name the function. Prefer e.g. `test_parse_docling_json_furniture_dropped` / `test_sections_to_record_record_shape`. Apply consistently if adopted.

## Skills with No Issues

1. unit-tests — pytest usage (1): No issues. Uses pytest, no `unittest`.
2. unit-tests — file naming (2.1): No issues. File is `test_docling_section_parser.py` for `docling_section_parser.py`.
3. unit-tests — Arrange-Act-Assert (3.2): No issues. Tests are cleanly three-phased.
4. unit-tests — fixtures/isolation (3.1, 6): No issues. Module-level helpers (`_prov`, `_table_data`, `_parse`) are pure builders, each test uses the per-test `tmp_path`, no shared mutable state or ordering dependency, and no assertions on private attributes. Helpers are not promoted to `conftest.py` fixtures, but the file-local `conftest.py` only handles path setup and these helpers are not shared across files, so leaving them module-local is acceptable.
5. unit-tests — mock external boundaries (4): N/A. The parser is exercised end-to-end against real temp-file JSON (the genuine boundary), which is preferable to mocking the filesystem here; no mocking needed.
6. unit-tests — DataFrame comparison (5.3): N/A. No DataFrames in this module.
7. unit-tests — comprehensive coverage (7, 7.1, 7.2): **Issues found** — see Implementation Plan items 1-4. Happy paths well covered; error conditions (1.x) and several edge/boundary behaviors (2.x, 3.1, 4.1) are missing. `--cov` confirms lines 213-220 and 257 uncovered.
8. unit-tests — parametrize (5.1): **Issue found** — see 1.4. No parametrization; the error-path tests are the natural candidate.
9. unit-tests — test exceptions (5.2): **Issue found** — see 1.1-1.3. No `pytest.raises` and no `match=` anywhere; the documented `OSError`/`ValueError` contract is unverified.

## Status & Next Steps

**Current Status**: Review complete. Suite passes (15 tests); 93% line coverage measured with `pytest-cov`; gaps are in error/edge paths only.
**Completed**:
1. Read the module under test and the test file in full.
2. Ran `pytest --cov=docling_section_parser --cov-report=term-missing`; verified missing lines 213-220 (error block) and 257 (blank-heading guard).
3. Mapped every required behavior to its covering test and severity-ranked the gaps.
**Next Steps**:
1. Add the error-path tests (1.1-1.3) using `pytest.raises(..., match=...)`; optionally parametrize (1.4).
2. Add the whole-document edge tests (2.1-2.3) and the word_count summation test (3.1).
3. Add the blank-heading guard test (4.1); optionally the explicit section_header test (4.2).
4. Re-run `pytest --cov` and confirm the module reaches full statement+branch coverage.
**Blockers**:
1. None. `pytest-cov` was available in the project venv (via `uv`); coverage was measured directly.
**Notes**:
1. Items 1.x, 2.x, and 3.1 are [major] because they are the behaviors the task flagged as most important and because the module's documented exception contract is entirely unverified — these rank above the naming/parametrization style items.
