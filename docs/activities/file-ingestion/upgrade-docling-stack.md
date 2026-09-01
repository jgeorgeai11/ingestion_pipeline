---
name: upgrade-docling-stack
goal: Upgrade the Docling library stack (docling, docling-core, docling-ibm-models, docling-parse) to current to fix a docling-parse native per-page virtual-memory over-commit bug that OOMs/segfaults file ingestion on the Linux VM, while pinning the model stack (transformers/torch) unchanged so the already-embedded corpus and the embedding/reranker models are unaffected. The pass criterion is that the previously-failing CMS Pub 100-01 chapter 7 (and the USC titles) parse on the VM without the memory blowup.
created: 2026-06-29
updated: 2026-06-29
---

## Implementation Plan

1. [completed] Constrain the model stack, then raise the Docling floor - `pyproject.toml`
   - 1.1. Hold the model-stack runtime so the docling bump cannot move it, via uv's constraint mechanism (`[tool.uv] constraint-dependencies`) rather than new direct dependencies — `transformers` and `torch` are transitive and NOT on the approved-packages list, so they must not be added with `uv add`. Constrain `transformers<5` and `torch` to its current line (~2.10). Necessary because docling-ibm-models 3.13.3 alone would allow transformers up to 5.8 and torch <3 — the model-stack jump deferred to activity #2.
   - 1.2. Raise the docling floor with `uv add 'docling>=2.107'` (docling is on the approved list; docling-core / docling-ibm-models / docling-parse arrive transitively via docling's own pins — no separate `uv add` for those).
   - 1.3. Let `uv add` re-resolve + install; record the resolved versions of docling / docling-core / docling-ibm-models / docling-parse / transformers / torch from `uv pip list`.
   - 1.4. Verify transformers and torch did NOT move off the constrained versions (`uv pip list`).

2. [completed] Reconcile the PDF-backend allow-list with the new Docling - `code/file_ingestion/file_parser.py`
   - 2.1. Enumerate the PDF backends docling 2.107 ships (the parse-backend class names + any new/deprecated ones) and the converter's own default.
   - 2.2. Update `VALID_PDF_BACKENDS` and the name->class resolution dict to match exactly what is importable (add the modern docling-parse backend; drop any removed one), keeping the assert that the allow-list and the dict cannot silently drift.

3. [completed] Set the default PDF backend to the fixed docling-parse engine - `code/file_ingestion/ingest.py`
   - 3.1. Change `DEFAULT_PDF_BACKEND` off `dlparse` (v1, the over-commit culprit) to the modern docling-parse backend on docling-parse 7.x (e.g. `dlparse_v4`); update the related comment.

4. [completed] Update the affected tests for the reconciled allow-list - `code/file_ingestion/unit_tests/test_file_parser.py`, `code/file_ingestion/unit_tests/test_ingest.py`
   - 4.1. `test_file_parser.py`: update the backend-validation tests (valid + invalid backend names) to the reconciled `VALID_PDF_BACKENDS`; keep the allow-list/resolution-dict drift assertion covered.
   - 4.2. `test_ingest.py`: the `_group_files_by_parse_settings` tests use the literal `"dlparse"` backend label; if v1 (`dlparse`) is dropped from the allow-list in Task 2, switch those labels to a still-valid backend, otherwise leave them. (Shared concern: both files are test updates for the same allow-list change.)
   - 4.3. Run `uv run pytest code/file_ingestion/unit_tests/ -q` to green.

5. [completed] Validate parse quality locally against the captured baseline - (data validation; no new code file)
   - 5.1. Re-parse `ge101c07.pdf` plus 2-3 representative docs (a USC title chapter, a qpp_cm MIF, another cms_iom chapter) with the upgraded docling-parse; compare pages / texts / tables / char-count to the pre-upgrade baseline (c07 = 100 pages / 1129 texts / 51 tables / 251,203 chars) and eyeball the cleaned sections for regressions.
   - 5.2. Record any structural diffs; flag a corpus re-parse only if output changed materially.

6. [completed] Confirm the model stack is unaffected - (validation; no new code file)
   - 6.1. Confirm transformers/torch versions are unchanged post-upgrade.
   - 6.2. Import-test `code/mcp_db_server/mcp_db_server.py`; confirm the granite embedding model loads and produces 384-dim vectors and the bge reranker loads. No re-embed is expected because the model stack did not change.
   - 6.3. Run the `mcp_db_server` test suite to confirm no import/regression.

7. [pending] VM validation gate (authoritative pass/fail) - (run on the Linux VM)
   - 7.1. On the VM, run the 100-01 ingestion (or just `ge101c07`) with the upgraded docling-parse + new default backend; confirm it parses WITHOUT OOM/segfault and that committed/resident memory stays sane (`/proc/meminfo` Committed_AS; process RSS).
   - 7.2. Run the other previously-failing configs (USC titles especially) to confirm the whole class of failure is resolved.

## Key Data Decisions and Considerations

1. Isolate the docling upgrade from the model stack - Pinning transformers (<5) and torch (<3) keeps this change parse-only. The latest docling stack explicitly supports transformers>=4.42 and torch>=2.2.2, so the current 4.57.6 / 2.10 satisfy it without moving. This avoids touching the embedding/reranker runtime and the ~800K already-embedded chunks; the transformers 4->5 / torch bump is deferred to a separate activity with an embedding-reproducibility gate.
2. Root cause is docling-parse, not document size or the table model - On the VM, v1 OOMs and v4 segfaults (both the docling-parse native path), while pypdfium2 parses the same file with the same 51 tables and ~equal text. The VM agent measured ~50 GB of virtual commit for a 100-page letter PDF (~500 MB/page) - a docling-parse over-reservation that macOS hides via lazy overcommit but Linux's commit limit rejects (-> OOM on v1, null-deref segfault on v4). A 2-major-version docling-parse bump (5.3.2 -> 7.2.0) is the durable fix.
3. The Mac cannot reproduce the failure - macOS overcommits virtual memory lazily, so the bug is invisible locally; local validation can only confirm parse quality and that nothing else broke. The authoritative pass criterion is the VM re-test (Task 7).
4. Backend naming may shift across the major bump - docling-parse 7.x may rename/retire the v1/v2/v4 backends or change the converter default; Task 2 reconciles the allow-list against what is actually importable rather than assuming the current three names.
5. pypdfium2 is the fallback - it already works on the VM with near-equal output (verified on c07: 1141 texts / 51 tables vs 1129 / 51); if the upgrade slips, switching DEFAULT_PDF_BACKEND to pypdfium2 is the zero-risk stopgap.
6. Package-management conventions (per the package-management skill) - Use `uv add` for the docling bump (docling and sentence-transformers are the approved-list packages; docling-core/docling-ibm-models/docling-parse are transitive). `transformers` and `torch` are transitive and NOT on the approved list, so they are held via `[tool.uv] constraint-dependencies` rather than added as direct deps. No new direct dependency is introduced by this activity.

## Status & Next Steps

**Current Status**: Tasks 1-6 implemented and validated locally on branch `docling-upgrade`; awaiting the Task 7 VM gate.
**Completed**:
1. Diagnosed the root cause (docling-parse native per-page virtual over-commit; v1 OOM / v4 segfault on the VM; pypdfium2 works with equal output).
2. Task 1 - upgraded docling 2.74->2.107, docling-core 2.65->2.85, docling-ibm-models 3.11->3.13.3, docling-parse 5.3.2->7.2.0; transformers (4.57.6) and torch (2.10.0) held via `[tool.uv] constraint-dependencies`.
3. Task 2 - added `dlparse_v4` to VALID_PDF_BACKENDS + the resolution dict in file_parser.py (docling 2.107 keeps v1/v2/v4; none removed).
4. Task 3 - DEFAULT_PDF_BACKEND in ingest.py set to `dlparse_v4`; example.toml backend comment updated.
5. Task 4 - updated test_file_parser.py (v4 mapping case) + test_ingest.py (hard-default fallback now `dlparse_v4`); 213 file_ingestion tests pass.
6. Task 5 - c07 on docling-parse 7.2.0 + v4 = 100 pages / 1122 texts / 51 tables / 250,555 chars vs the 5.3.2 baseline 100 / 1129 / 51 / 251,203 (identical pages + tables, -0.6% texts, -0.26% chars) - equivalent, no re-parse needed.
7. Task 6 - 67 mcp_db_server tests pass; granite embedding loads at dim 384; bge reranker loads - model stack unaffected, no re-embed.
**Next Steps**:
1. Task 7 (VM gate) - on the Linux VM, run the 100-01 ingestion (and the USC titles) with docling-parse 7.2.0 + dlparse_v4; confirm no OOM/segfault and sane Committed_AS / RSS.
2. Optional - refresh the stale commented `pdf_backend` lines in the per-pub/per-title configs (they still read default "dlparse"; harmless since commented out).
**Blockers**:
1. Final pass/fail depends on the Task 7 VM re-test, which cannot run on macOS (the bug only manifests under Linux's commit limit).
**Notes**:
1. Follow-up activity (separate branch): model-stack upgrade (transformers 4->5, torch, sentence-transformers) with a re-embed/reproducibility gate.
2. Out of scope here: the uncommitted `readme/` pair and the model-stack upgrade.
3. Reviewed against the activity-development + package-management guidelines: switched Task 1 to `uv add` + `[tool.uv] constraint-dependencies` (no new direct deps; respects the approved-packages list), tightened the transformers/torch hold, and added `test_ingest.py` to the test task (it references the `dlparse` backend label).
4. CORRECTION (in the page-range-batched-parsing activity): Tasks 2-3 chose `dlparse_v4` as the backend/default, but it (and `dlparse_v2`) turned out to be a deprecated subclass of `dlparse` (v1) that warns and will raise in a future docling release. The default was reverted to `dlparse` and the allow-list trimmed to (`pypdfium2`, `dlparse`). v4 is behaviorally identical to v1, so this is a no-op functionally - the per-page over-commit is bounded by page-range batching, not the backend. See docs/activities/file-ingestion/page-range-batched-parsing.md Key Decision 7.
