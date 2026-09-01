"""Tests for the quality_report module.

Each check function is exercised in isolation with synthetic inputs. The parsed
side is built from small ``DoclingDocument`` instances via the ``add_*``
builders, round-tripped through ``save_as_json`` / ``load_from_json`` (mirroring
``test_docling_section_parser.py``); the cleaned side is plain dicts shaped like
the ``CleanedDocument`` JSON the clean step writes.

Naming convention: tests follow ``test_<scenario>_<expected>`` and rely on the
enclosing ``Test<Function>`` class for the function-under-test segment (one
class per check function), so the target is unambiguous without repeating the
function name in every method.
"""

import json as _json
from pathlib import Path
from typing import Any

import pytest
from docling_core.types.doc import DocItemLabel, DoclingDocument
from docling_core.types.doc.base import BoundingBox, CoordOrigin
from docling_core.types.doc.document import (
    DocumentOrigin,
    ProvenanceItem,
    TableCell,
    TableData,
)
from ingpipe_file_ingestion import quality_report as quality_report_module
from ingpipe_file_ingestion.quality_report import (
    _classify_pages,
    _normalize_tokens,
    _render_console,
    _section_text_health,
    analyze_document,
    build_report,
    check_element_types,
    check_page_coverage,
    check_table_coverage,
    check_text_coverage,
    check_text_health,
    compute_fragmentation_metrics,
    flag_fragmentation,
)

FIXTURE_BINARY_HASH = 12345678901234567890


def _origin() -> DocumentOrigin:
    """Build a DocumentOrigin carrying the fixture's known binary_hash.

    Returns:
        A DocumentOrigin with a fixed mimetype/filename and FIXTURE_BINARY_HASH.
    """
    return DocumentOrigin(
        mimetype="application/pdf",
        binary_hash=FIXTURE_BINARY_HASH,
        filename="fixture.pdf",
    )


def _prov(page_no: int) -> ProvenanceItem:
    """Build a minimal provenance item on the given page.

    Args:
        page_no: 1-based page number to record.

    Returns:
        A ProvenanceItem with a unit bounding box and empty char span.
    """
    return ProvenanceItem(
        page_no=page_no,
        bbox=BoundingBox(l=0, t=0, r=1, b=1, coord_origin=CoordOrigin.TOPLEFT),
        charspan=(0, 0),
    )


def _table_data(rows: list[list[str]]) -> TableData:
    """Build a TableData grid from a list of string rows.

    Args:
        rows: Row-major cell text.

    Returns:
        A TableData populated with one cell per value.
    """
    num_rows = len(rows)
    num_cols = len(rows[0]) if rows else 0
    cells: list[TableCell] = []
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            cells.append(
                TableCell(
                    text=value,
                    row_span=1,
                    col_span=1,
                    start_row_offset_idx=r,
                    end_row_offset_idx=r + 1,
                    start_col_offset_idx=c,
                    end_col_offset_idx=c + 1,
                )
            )
    return TableData(num_rows=num_rows, num_cols=num_cols, table_cells=cells)


def _roundtrip(doc: DoclingDocument, tmp_path: Path) -> DoclingDocument:
    """Serialize a fixture document and load it back as a DoclingDocument.

    Args:
        doc: The fixture document to serialize.
        tmp_path: pytest temp directory for the JSON file.

    Returns:
        The reloaded DoclingDocument (matching how the report reads parse JSON).
    """
    if doc.origin is None:
        doc.origin = _origin()
    json_path = tmp_path / "fixture.json"
    doc.save_as_json(json_path)
    return DoclingDocument.load_from_json(json_path)


def _cleaned(sections: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap section dicts into a cleaned-document envelope.

    Args:
        sections: Section dicts (need only the keys a check reads).

    Returns:
        A ``{"document": ..., "sections": ...}`` dict.
    """
    return {
        "document": {"n_parsed_sections": len(sections), "binary_hash": 0},
        "sections": sections,
    }


class TestNormalizeTokens:
    """Tests for the shared _normalize_tokens helper (Checks 1 & 2)."""

    def test_decimal_normalization(self) -> None:
        """Trailing zeros after a decimal are stripped; integers are preserved."""
        tokens = _normalize_tokens("Pay 89.50 and 92.00 over 100 years in 2020")
        assert "89.5" in tokens
        assert "92" in tokens
        # The raw trailing-zero forms must be absent — proves the rewrite
        # happened rather than emitting both forms.
        assert "89.50" not in tokens
        assert "92.00" not in tokens
        # Integers must survive intact (the rstrip-only-decimals guard).
        assert "100" in tokens
        assert "2020" in tokens

    def test_case_insensitive(self) -> None:
        """Tokens are lowercased so case never causes a coverage gap."""
        assert _normalize_tokens("Hello WORLD") == {"hello", "world"}


class TestTextCoverage:
    """Tests for Check 1 (text coverage excluding tables)."""

    def test_detects_dropped_token(self, tmp_path: Path) -> None:
        """A parsed token absent from the cleaned set is reported as missing."""
        doc = DoclingDocument(name="t", origin=_origin())
        doc.add_heading(text="Heading", level=1, prov=_prov(1))
        doc.add_text(label=DocItemLabel.TEXT, text="alpha beta gamma", prov=_prov(1))
        reloaded = _roundtrip(doc, tmp_path)

        # Cleaned content is missing "gamma".
        cleaned = _cleaned(
            [{"heading_text": "Heading", "content_text": "alpha beta"}]
        )
        result = check_text_coverage(reloaded, cleaned)

        assert result["missing_tokens"] == ["gamma"]
        assert result["missing_count"] == 1

    def test_passes_when_covered(self, tmp_path: Path) -> None:
        """No missing tokens when the cleaned set covers all parsed text."""
        doc = DoclingDocument(name="t", origin=_origin())
        doc.add_heading(text="Heading", level=1, prov=_prov(1))
        doc.add_text(label=DocItemLabel.TEXT, text="alpha beta", prov=_prov(1))
        reloaded = _roundtrip(doc, tmp_path)

        cleaned = _cleaned(
            [{"heading_text": "Heading", "content_text": "alpha beta gamma"}]
        )
        assert check_text_coverage(reloaded, cleaned)["missing_count"] == 0

    def test_number_normalization_no_false_flag(self, tmp_path: Path) -> None:
        """89.50 in the parse and 89.5 in the clean must NOT flag (normalization)."""
        doc = DoclingDocument(name="t", origin=_origin())
        doc.add_text(label=DocItemLabel.TEXT, text="amount 89.50 due", prov=_prov(1))
        reloaded = _roundtrip(doc, tmp_path)

        cleaned = _cleaned([{"heading_text": None, "content_text": "amount 89.5 due"}])
        assert check_text_coverage(reloaded, cleaned)["missing_count"] == 0


class TestTableCoverage:
    """Tests for Check 2 (table-cell content coverage)."""

    def test_detects_dropped_cell(self, tmp_path: Path) -> None:
        """A table cell token absent from cleaned content is reported per table."""
        doc = DoclingDocument(name="t", origin=_origin())
        doc.add_table(data=_table_data([["alpha", "beta"], ["gamma", "delta"]]), prov=_prov(3))
        reloaded = _roundtrip(doc, tmp_path)

        # Cleaned content omits "delta".
        cleaned = _cleaned(
            [{"heading_text": None, "content_text": "alpha beta gamma"}]
        )
        result = check_table_coverage(reloaded, cleaned)

        assert result["table_count"] == 1
        assert len(result["tables"]) == 1
        assert result["tables"][0]["missing_tokens"] == ["delta"]
        assert result["tables"][0]["reading_order_index"] == 1
        assert result["tables"][0]["page"] == 3

    def test_passes_when_cells_covered(self, tmp_path: Path) -> None:
        """No flagged tables when every cell token is present in cleaned content."""
        doc = DoclingDocument(name="t", origin=_origin())
        doc.add_table(data=_table_data([["alpha", "beta"]]), prov=_prov(1))
        reloaded = _roundtrip(doc, tmp_path)

        cleaned = _cleaned([{"heading_text": None, "content_text": "alpha beta"}])
        assert check_table_coverage(reloaded, cleaned)["tables"] == []

    def test_blank_grid_uses_caption(self, tmp_path: Path) -> None:
        """A blank-grid table's expected text is its caption (the cleaner's path)."""
        doc = DoclingDocument(name="t", origin=_origin())
        caption = doc.add_text(
            label=DocItemLabel.CAPTION, text="Table caption tokens", prov=_prov(2)
        )
        doc.add_table(data=_table_data([["", ""]]), caption=caption, prov=_prov(2))
        reloaded = _roundtrip(doc, tmp_path)

        # Cleaned content omits the caption word "tokens".
        cleaned = _cleaned([{"heading_text": None, "content_text": "Table caption"}])
        result = check_table_coverage(reloaded, cleaned)

        assert len(result["tables"]) == 1
        assert "tokens" in result["tables"][0]["missing_tokens"]


class TestElementTypes:
    """Tests for Check 3 (element-type audit)."""

    def test_flags_unhandled_type(self, tmp_path: Path) -> None:
        """A KeyValueItem is unhandled and surfaced in the inventory.

        KeyValueItem is not in any handled label set and is not a
        Table/Picture/Text subclass, so it is unhandled. It is text-less (the
        common inventory case the audit exists to surface), so carries_text is
        False — the runtime guard only blocks text-bearing unhandled elements.
        """
        from docling_core.types.doc.document import GraphData

        doc = DoclingDocument(name="t", origin=_origin())
        doc.add_text(label=DocItemLabel.TEXT, text="body", prov=_prov(1))
        doc.add_key_values(graph=GraphData(cells=[], links=[]))
        reloaded = _roundtrip(doc, tmp_path)

        result = check_element_types(reloaded)
        unhandled_labels = {u["label"] for u in result["unhandled"]}

        assert "key_value_region" in unhandled_labels
        flagged = next(u for u in result["unhandled"] if u["label"] == "key_value_region")
        assert flagged["carries_text"] is False

    def test_text_bearing_unhandled_element_flags_carries_text(self) -> None:
        """A text-bearing unhandled element surfaces with carries_text True.

        A text-bearing unhandled element is the "real hazard" the audit exists
        to surface (vs. a benign text-less structural container). Empirically no
        ``DocItem`` subclass is BOTH unhandled (not a Table/Picture/Text subclass
        and not in any handled label set) AND text-bearing, so this defensive
        branch is not reachable via the real ``add_*`` builders. A minimal stub
        doc/item is therefore used to drive the actual ``check_element_types``
        logic (the OR-accumulation guard) so the produced ``carries_text=True``
        is exercised against the real code path rather than a serialized fixture.
        """

        class _UnhandledTextItem:
            """A minimal unhandled, text-bearing Docling-like item."""

            label = "custom_region"  # not in FURNITURE/DROP/HEADING label sets
            text = "hazard text"

        class _StubDoc:
            """A minimal doc exposing only the iterate_items seam the check uses."""

            def iterate_items(self) -> Any:
                """Yield the single unhandled text-bearing item with its level."""
                yield (_UnhandledTextItem(), 0)

        result = check_element_types(_StubDoc())
        unhandled = result["unhandled"]

        assert len(unhandled) == 1
        entry = unhandled[0]
        assert entry["type"] == "_UnhandledTextItem"
        assert entry["label"] == "custom_region"
        assert entry["carries_text"] is True

    def test_handled_text_not_flagged(self, tmp_path: Path) -> None:
        """Ordinary text/heading/table elements are not reported as unhandled.

        Also pins the per-document ``census`` rows (type/label/count) so a
        regression in census counting is caught alongside the unhandled list.
        """
        doc = DoclingDocument(name="t", origin=_origin())
        doc.add_heading(text="Heading", level=1, prov=_prov(1))
        doc.add_text(label=DocItemLabel.TEXT, text="body", prov=_prov(1))
        doc.add_table(data=_table_data([["a", "b"]]), prov=_prov(1))
        reloaded = _roundtrip(doc, tmp_path)

        result = check_element_types(reloaded)
        assert result["unhandled"] == []
        census = {(e["type"], e["label"]): e["count"] for e in result["census"]}
        assert census == {
            ("SectionHeaderItem", "section_header"): 1,
            ("TextItem", "text"): 1,
            ("TableItem", "table"): 1,
        }


class TestPageCoverage:
    """Tests for Check 4 (page coverage)."""

    def test_computes_ratio_and_classifies_zero_element_page(
        self, tmp_path: Path
    ) -> None:
        """An uncovered page with no elements is classified zero-elements."""
        from docling_core.types.doc.base import Size
        from docling_core.types.doc.document import PageItem

        size = Size(width=1, height=1)
        doc = DoclingDocument(name="t", origin=_origin())
        # Body on pages 1 and 2; page 3 has no provenance-bearing element.
        doc.add_text(label=DocItemLabel.TEXT, text="page one", prov=_prov(1))
        doc.add_text(label=DocItemLabel.TEXT, text="page two", prov=_prov(2))
        reloaded = _roundtrip(doc, tmp_path)
        # Register three pages so total_pages is 3 with page 3 empty (the parser
        # found nothing on it -> zero-elements once it is left uncovered).
        reloaded.pages = {
            1: PageItem(page_no=1, size=size),
            2: PageItem(page_no=2, size=size),
            3: PageItem(page_no=3, size=size),
        }

        cleaned = _cleaned(
            [{"heading_text": None, "content_text": "covered", "page_start": 1, "page_end": 2}]
        )
        result = check_page_coverage(reloaded, cleaned)

        assert result["total_pages"] == 3
        assert result["covered_pages"] == 2
        assert result["coverage"] == round(2 / 3, 4)
        assert result["classification"] == "paginated"
        uncovered = {e["page"]: e["classification"] for e in result["uncovered"]}
        assert uncovered == {3: "zero-elements"}
        assert result["has_uncovered_zero_element"] is True
        assert result["classification_breakdown"] == {"zero-elements": 1}

    def test_blank_table_only_page_is_content_less(self, tmp_path: Path) -> None:
        """A page whose only element is a blank, caption-less table is benign.

        The cleaner drops an all-blank, caption-less table, so the page is
        content-less, not lost content. It must classify as ``blank-table`` (not
        ``has-body``) and, when uncovered, must NOT raise the has-body signal.
        """
        doc = DoclingDocument(name="t", origin=_origin())
        doc.add_text(label=DocItemLabel.TEXT, text="page one", prov=_prov(1))
        # Page 2's only element is an all-blank table (no caption).
        doc.add_table(data=_table_data([["", ""], ["", ""]]), prov=_prov(2))
        reloaded = _roundtrip(doc, tmp_path)

        cleaned = _cleaned(
            [{"heading_text": None, "content_text": "page one", "page_start": 1, "page_end": 1}]
        )
        result = check_page_coverage(reloaded, cleaned)

        uncovered = {e["page"]: e["classification"] for e in result["uncovered"]}
        assert uncovered == {2: "blank-table"}
        assert result["has_uncovered_has_body"] is False
        assert result["has_uncovered_zero_element"] is False
        assert result["classification_breakdown"] == {"blank-table": 1}

    def test_nonblank_table_page_is_has_body(self, tmp_path: Path) -> None:
        """A page with a non-blank table classifies as has-body (real content)."""
        doc = DoclingDocument(name="t", origin=_origin())
        doc.add_text(label=DocItemLabel.TEXT, text="page one", prov=_prov(1))
        # Page 2 carries a table with real cell content.
        doc.add_table(data=_table_data([["alpha", "beta"]]), prov=_prov(2))
        reloaded = _roundtrip(doc, tmp_path)

        # Page 2 is left uncovered so the has-body signal can fire.
        cleaned = _cleaned(
            [{"heading_text": None, "content_text": "page one", "page_start": 1, "page_end": 1}]
        )
        result = check_page_coverage(reloaded, cleaned)

        uncovered = {e["page"]: e["classification"] for e in result["uncovered"]}
        assert uncovered == {2: "has-body"}
        assert result["has_uncovered_has_body"] is True
        assert result["has_uncovered_zero_element"] is False

    def test_real_text_page_is_has_body(self, tmp_path: Path) -> None:
        """A page with non-empty text classifies as has-body."""
        doc = DoclingDocument(name="t", origin=_origin())
        doc.add_text(label=DocItemLabel.TEXT, text="real content here", prov=_prov(1))
        reloaded = _roundtrip(doc, tmp_path)

        # Nothing covered, so page 1 is an uncovered has-body page.
        cleaned = _cleaned([{"heading_text": None, "content_text": "elsewhere"}])
        result = check_page_coverage(reloaded, cleaned)

        uncovered = {e["page"]: e["classification"] for e in result["uncovered"]}
        assert uncovered == {1: "has-body"}
        assert result["has_uncovered_has_body"] is True

    def test_caption_only_page_is_not_has_body(self, tmp_path: Path) -> None:
        """A page whose only element is a standalone caption is content-less.

        Standalone captions are drop-labels the cleaner discards, so a page that
        carries only one must NOT classify as has-body (the has-body ⟺
        real-content invariant); it falls to furniture-only.
        """
        doc = DoclingDocument(name="t", origin=_origin())
        doc.add_text(label=DocItemLabel.TEXT, text="page one", prov=_prov(1))
        # Page 2's only element is a standalone caption (a DROP_LABEL).
        doc.add_text(label=DocItemLabel.CAPTION, text="Figure 1 caption", prov=_prov(2))
        reloaded = _roundtrip(doc, tmp_path)

        cleaned = _cleaned(
            [{"heading_text": None, "content_text": "page one", "page_start": 1, "page_end": 1}]
        )
        result = check_page_coverage(reloaded, cleaned)

        uncovered = {e["page"]: e["classification"] for e in result["uncovered"]}
        assert uncovered == {2: "furniture-only"}
        assert result["has_uncovered_has_body"] is False

    def test_caption_less_picture_page_is_image_only(self, tmp_path: Path) -> None:
        """A page whose only element is a caption-less picture is image-only.

        A caption-less picture carries no keepable content, so the page is a
        benign content-less ``image-only`` page (not ``has-body``); when
        uncovered it must NOT raise the has-body signal.
        """
        doc = DoclingDocument(name="t", origin=_origin())
        doc.add_text(label=DocItemLabel.TEXT, text="page one", prov=_prov(1))
        # Page 2's only element is a caption-less picture.
        doc.add_picture(prov=_prov(2))
        reloaded = _roundtrip(doc, tmp_path)

        cleaned = _cleaned(
            [{"heading_text": None, "content_text": "page one", "page_start": 1, "page_end": 1}]
        )
        result = check_page_coverage(reloaded, cleaned)

        uncovered = {e["page"]: e["classification"] for e in result["uncovered"]}
        assert uncovered == {2: "image-only"}
        assert result["has_uncovered_has_body"] is False
        assert result["has_uncovered_zero_element"] is False
        assert result["classification_breakdown"] == {"image-only": 1}

    def test_captioned_picture_page_is_has_body(self, tmp_path: Path) -> None:
        """A page whose only element is a captioned picture is has-body.

        The cleaner keeps a captioned picture, so its caption is real content;
        the page must classify as ``has-body`` and, when uncovered, raise the
        true content-loss signal.
        """
        doc = DoclingDocument(name="t", origin=_origin())
        doc.add_text(label=DocItemLabel.TEXT, text="page one", prov=_prov(1))
        # Page 2's only element is a picture carrying a caption (real content).
        caption = doc.add_text(
            label=DocItemLabel.CAPTION, text="Figure 1 caption", prov=_prov(2)
        )
        doc.add_picture(caption=caption, prov=_prov(2))
        reloaded = _roundtrip(doc, tmp_path)

        cleaned = _cleaned(
            [{"heading_text": None, "content_text": "page one", "page_start": 1, "page_end": 1}]
        )
        result = check_page_coverage(reloaded, cleaned)

        uncovered = {e["page"]: e["classification"] for e in result["uncovered"]}
        assert uncovered == {2: "has-body"}
        assert result["has_uncovered_has_body"] is True
        assert result["has_uncovered_zero_element"] is False

    def test_non_paginated_source_coverage_na(self, tmp_path: Path) -> None:
        """A doc with no page model yields coverage n/a, not a false 0.0.

        The fixture carries elements with NO provenance and registers no pages,
        so total_pages degenerates to 0 (the Word/DOCX case). Coverage must be
        None with classification ``non-paginated`` and no coverage flags.
        """
        doc = DoclingDocument(name="t", origin=_origin())
        # No prov -> no page_no; combined with zero registered pages this is the
        # non-paginated signature.
        doc.add_text(label=DocItemLabel.TEXT, text="word document body text")
        reloaded = _roundtrip(doc, tmp_path)

        cleaned = _cleaned([{"heading_text": None, "content_text": "word document body text"}])
        result = check_page_coverage(reloaded, cleaned)

        assert result["total_pages"] == 0
        assert result["coverage"] is None
        assert result["classification"] == "non-paginated"
        assert result["uncovered"] == []
        assert result["has_uncovered_zero_element"] is False
        assert result["has_uncovered_has_body"] is False


class TestFragmentation:
    """Tests for Check 5 (fragmentation extremes, two-pass)."""

    def test_flags_over_fragmented_outlier(self) -> None:
        """An over-fragmented doc (high sections/page) is flagged vs the corpus."""
        # A corpus of normal docs plus one over-fragmented outlier.
        normal = _cleaned(
            [
                {"heading_text": "h", "content_text": "many words here indeed yes", "word_count": 6}
                for _ in range(5)
            ]
        )
        outlier = _cleaned(
            [{"heading_text": "h", "content_text": None, "word_count": 1} for _ in range(60)]
        )

        per_doc = {
            "n1": compute_fragmentation_metrics(normal, total_pages=5),
            "n2": compute_fragmentation_metrics(normal, total_pages=5),
            "n3": compute_fragmentation_metrics(normal, total_pages=5),
            "n4": compute_fragmentation_metrics(normal, total_pages=5),
            "out": compute_fragmentation_metrics(outlier, total_pages=5),
        }
        flags = flag_fragmentation(per_doc)

        # Only the outlier is flagged — pins that the fences did not mis-fire and
        # flag the normal docs too (a tautology guard for the collapsed fence).
        assert set(flags) == {"out"}
        assert any("over-fragmentation" in r for r in flags["out"]["reasons"])

    def test_non_paginated_doc_not_flagged_on_sections_per_page(self) -> None:
        """A non-paginated doc is excluded from the sections_per_page fence.

        A Word/DOCX doc has total_pages == 0, so its sections_per_page is
        undefined. In a mixed corpus it must not present an inflated
        sections_per_page and false-flag as over-fragmented; it is excluded from
        both the fence and the over/under-fragmentation flag (mirroring the
        coverage-fence exclusion). The paginated outlier still flags.
        """
        normal = _cleaned(
            [
                {"heading_text": "h", "content_text": "many words here indeed yes", "word_count": 6}
                for _ in range(5)
            ]
        )
        # A large non-paginated doc: many sections but NO page count. Under the
        # old pages=1 fallback this would have sections_per_page == 60 and
        # false-flagged as an over-fragmentation outlier.
        big_non_paginated = _cleaned(
            [{"heading_text": "h", "content_text": "many words here indeed yes", "word_count": 6} for _ in range(60)]
        )

        per_doc = {
            "n1": compute_fragmentation_metrics(normal, total_pages=5),
            "n2": compute_fragmentation_metrics(normal, total_pages=5),
            "n3": compute_fragmentation_metrics(normal, total_pages=5),
            "n4": compute_fragmentation_metrics(normal, total_pages=5),
            "word_doc": compute_fragmentation_metrics(big_non_paginated, total_pages=0),
        }
        flags = flag_fragmentation(per_doc)

        # The non-paginated doc is not flagged on sections_per_page; with a
        # word-per-section equal to the corpus it is not flagged at all.
        assert "word_doc" not in flags


class TestTextHealth:
    """Tests for Check 6 (prose text-health)."""

    def test_flags_single_char_word_heavy_section(self) -> None:
        """A section dominated by single-ALPHABETIC-char words is flagged.

        Spaced-out letters ("S e l f") are the garbling signature the ratio
        targets.
        """
        # Ratio = 8/9 single-letter tokens, well above the 0.15 threshold.
        cleaned = _cleaned(
            [{"heading_text": "h", "content_text": "a b c d e f g h word"}]
        )
        result = check_text_health(cleaned)

        assert result["flagged_count"] == 1
        assert result["flagged_sections"][0]["single_char_word_ratio"] > 0.15

    def test_single_digits_not_counted(self) -> None:
        """Single DIGITS/punctuation do not count, so numeric prose is not flagged.

        A numeric-heavy section (revision codes, dollar figures spaced out as
        single digits) is benign content, not garbling, and must NOT flag.
        """
        # Every short token is a single digit; only single LETTERS count.
        cleaned = _cleaned(
            [{"heading_text": "h", "content_text": "1 2 3 4 5 6 7 8 amount due"}]
        )
        result = check_text_health(cleaned)

        assert result["flagged_count"] == 0
        # The metric itself reports a zero ratio (digits excluded), confirming
        # the fix is at the source, not a threshold tweak.
        metrics = _section_text_health("1 2 3 4 5 6 7 8 amount due")
        assert metrics["single_char_word_ratio"] == 0.0

    def test_broken_hyphenation_threshold(self) -> None:
        """Hyphenation flags only well above the benign line-break mass.

        A handful of line-break hyphen-splits in normal prose is a benign PDF
        artifact and must NOT flag; only a section riddled with them does.
        """
        # 4 broken hyphenations — benign line-break artifact, below threshold.
        benign = " ".join(["benefi- ciary"] * 4) + " ordinary closing prose words"
        assert check_text_health(_cleaned([{"heading_text": "h", "content_text": benign}]))[
            "flagged_count"
        ] == 0

        # Exactly 10 broken hyphenations — the boundary. The guard is strictly
        # ``> BROKEN_HYPHENATION_THRESHOLD`` (10), so 10 must NOT flag (pins the
        # off-by-one edge that the 4-vs-11 cases leave untested).
        boundary = " ".join(["benefi- ciary"] * 10) + " ordinary closing prose words"
        boundary_result = check_text_health(
            _cleaned([{"heading_text": "h", "content_text": boundary}])
        )
        assert boundary_result["flagged_count"] == 0
        assert _section_text_health(boundary)["broken_hyphenations"] == 10

        # 11 broken hyphenations — above BROKEN_HYPHENATION_THRESHOLD (10).
        suspect = " ".join(["benefi- ciary"] * 11)
        result = check_text_health(_cleaned([{"heading_text": "h", "content_text": suspect}]))
        assert result["flagged_count"] == 1
        assert result["flagged_sections"][0]["broken_hyphenations"] == 11

    def test_flags_replacement_char_section(self) -> None:
        """Any replacement char in the prose flags the section."""
        cleaned = _cleaned(
            [{"heading_text": "h", "content_text": "normal prose with a � glitch"}]
        )
        result = check_text_health(cleaned)

        assert result["flagged_count"] == 1
        assert result["flagged_sections"][0]["replacement_control_chars"] == 1

    def test_ignores_table_markdown(self) -> None:
        """Markdown-table lines are stripped so table syntax does not flag prose."""
        # The pipes/dashes would inflate single-char-word ratio if not stripped;
        # the surrounding prose is healthy, so nothing should flag.
        content = (
            "This is healthy descriptive prose about the policy rules here.\n\n"
            "| A | B | C |\n| --- | --- | --- |\n| 1 | 2 | 3 |"
        )
        cleaned = _cleaned([{"heading_text": "h", "content_text": content}])
        assert check_text_health(cleaned)["flagged_count"] == 0

    def test_newlines_not_flagged_as_control_chars(self) -> None:
        """Ordinary newlines/tabs are not counted as control chars."""
        cleaned = _cleaned(
            [{"heading_text": "h", "content_text": "line one prose\n\nline two prose"}]
        )
        assert check_text_health(cleaned)["flagged_count"] == 0


# ---------------------------------------------------------------------------
# analyze_document / build_report / console / main
# ---------------------------------------------------------------------------


def _full_section(sort_order: int, content: str, page: int) -> dict[str, Any]:
    """A cleaned section carrying every key the checks read."""
    return {
        "sort_order": sort_order,
        "heading_text": None,
        "content_text": content,
        "word_count": len(content.split()),
        "page_start": page,
        "page_end": page,
    }


def _write_document_pair(
    parsed_dir: Path, cleaned_dir: Path, stem: str, text: str = "hello world"
) -> None:
    """Write a matching parsed Docling JSON + cleaned JSON for one stem."""
    parsed_dir.mkdir(parents=True, exist_ok=True)
    cleaned_dir.mkdir(parents=True, exist_ok=True)
    doc = DoclingDocument(name=stem, origin=_origin())
    doc.add_text(label=DocItemLabel.TEXT, text=text, prov=_prov(1))
    doc.save_as_json(parsed_dir / f"{stem}.json")
    cleaned = {
        "document": {"n_parsed_sections": 1, "binary_hash": FIXTURE_BINARY_HASH},
        "sections": [_full_section(1, text, 1)],
    }
    (cleaned_dir / f"{stem}.json").write_text(
        _json.dumps(cleaned), encoding="utf-8"
    )


class TestClassifyPagesTotal:
    """Regression for the max(doc.pages) fix (activity task 20.5)."""

    def test_non_contiguous_page_keys_use_max_not_len(
        self, tmp_path: Path
    ) -> None:
        """Pages {1, 3} must report total_pages 3, not the key count 2.

        Batched parses stitch page-range slices back, so page keys need not
        be contiguous; len(doc.pages) would understate the extent.
        """
        from docling_core.types.doc.base import Size
        from docling_core.types.doc.document import PageItem

        size = Size(width=1, height=1)
        doc = DoclingDocument(name="t", origin=_origin())
        doc.add_text(label=DocItemLabel.TEXT, text="page one", prov=_prov(1))
        reloaded = _roundtrip(doc, tmp_path)
        reloaded.pages = {
            1: PageItem(page_no=1, size=size),
            3: PageItem(page_no=3, size=size),
        }

        total_pages, _classification = _classify_pages(reloaded)

        assert total_pages == 3


class TestAnalyzeDocument:
    """Per-document orchestration and its isolation boundary."""

    def test_happy_path_runs_every_check(self, tmp_path: Path) -> None:
        """A well-formed pair yields every check's result and no error."""
        parsed = tmp_path / "parsed"
        cleaned = tmp_path / "cleaned"
        _write_document_pair(parsed, cleaned, "docA")

        result = analyze_document("docA", parsed, cleaned, False)

        assert "error" not in result
        for key in (
            "text_coverage", "table_coverage", "element_types",
            "page_coverage", "text_health", "fragmentation",
        ):
            assert key in result
        # The opt-in check stays off by default (corpus-specific heuristic).
        assert "section_numbers" not in result

    def test_section_number_check_is_opt_in(self, tmp_path: Path) -> None:
        """The section-number heuristic runs ONLY when explicitly enabled."""
        parsed = tmp_path / "parsed"
        cleaned = tmp_path / "cleaned"
        _write_document_pair(parsed, cleaned, "docA")

        result = analyze_document("docA", parsed, cleaned, True)

        assert "section_numbers" in result

    def test_missing_parsed_and_cleaned_are_error_entries(
        self, tmp_path: Path
    ) -> None:
        """Missing inputs become per-document error entries, never raises."""
        parsed = tmp_path / "parsed"
        cleaned = tmp_path / "cleaned"
        parsed.mkdir()
        cleaned.mkdir()

        result = analyze_document("ghost", parsed, cleaned, False)
        assert "parsed JSON not found" in result["error"]

        (parsed / "ghost.json").write_text("{}", encoding="utf-8")
        result = analyze_document("ghost", parsed, cleaned, False)
        assert "cleaned JSON not found" in result["error"]

    def test_malformed_inputs_are_error_entries(self, tmp_path: Path) -> None:
        """Malformed parsed or cleaned JSON becomes an error entry."""
        parsed = tmp_path / "parsed"
        cleaned = tmp_path / "cleaned"
        parsed.mkdir()
        cleaned.mkdir()
        (parsed / "bad.json").write_text("{not-a-docling-doc}", encoding="utf-8")
        (cleaned / "bad.json").write_text("{}", encoding="utf-8")

        result = analyze_document("bad", parsed, cleaned, False)
        assert "could not load parsed JSON" in result["error"]

    def test_check_failure_is_isolated_per_document(self, tmp_path: Path) -> None:
        """A cleaned dict of the wrong shape yields an error entry, not a crash.

        The report-not-gate contract: a single bad document must never crash
        the run or change the exit code.
        """
        parsed = tmp_path / "parsed"
        cleaned = tmp_path / "cleaned"
        _write_document_pair(parsed, cleaned, "docA")
        # Overwrite the cleaned side with a shape the checks cannot consume.
        (cleaned / "docA.json").write_text('{"sections": 42}', encoding="utf-8")

        result = analyze_document("docA", parsed, cleaned, False)

        assert "check failed" in result["error"]


class TestBuildReport:
    """Corpus report assembly (flags, census, fence, ranking)."""

    def _results(self, tmp_path: Path) -> list[dict[str, Any]]:
        """Two clean documents plus one error entry."""
        parsed = tmp_path / "parsed"
        cleaned = tmp_path / "cleaned"
        _write_document_pair(parsed, cleaned, "docA")
        _write_document_pair(parsed, cleaned, "docB", text="beta body text")
        results = [
            analyze_document("docA", parsed, cleaned, False),
            analyze_document("docB", parsed, cleaned, False),
            {"stem": "broken", "error": "parsed JSON not found: x"},
        ]
        return results

    def test_counts_census_and_errors(self, tmp_path: Path) -> None:
        """Counts, the element census, and the error list are assembled."""
        report = build_report(self._results(tmp_path), "cfg_stem")

        assert report["config_stem"] == "cfg_stem"
        assert report["documents_total"] == 3
        assert report["documents_ok"] == 2
        assert report["documents_error"] == 1
        assert report["errors"] == [
            {"stem": "broken", "error": "parsed JSON not found: x"}
        ]
        # The census aggregates the shared text element across both documents.
        text_rows = [e for e in report["element_type_census"] if e["label"] == "text"]
        assert text_rows and text_rows[0]["n_docs"] == 2

    def test_flagged_documents_ranked_by_flag_count(self, tmp_path: Path) -> None:
        """A document with a dropped token is flagged; clean ones are not."""
        parsed = tmp_path / "parsed"
        cleaned = tmp_path / "cleaned"
        _write_document_pair(parsed, cleaned, "clean_doc")
        # Lossy doc: parsed text carries a token the cleaned side dropped.
        _write_document_pair(parsed, cleaned, "lossy_doc", text="alpha beta")
        lossy_cleaned = {
            "document": {"n_parsed_sections": 1, "binary_hash": FIXTURE_BINARY_HASH},
            "sections": [_full_section(1, "alpha", 1)],
        }
        (cleaned / "lossy_doc.json").write_text(
            _json.dumps(lossy_cleaned), encoding="utf-8"
        )
        results = [
            analyze_document("clean_doc", parsed, cleaned, False),
            analyze_document("lossy_doc", parsed, cleaned, False),
        ]

        report = build_report(results, "cfg")

        flagged = report["flagged_for_review"]
        assert [f["stem"] for f in flagged] == ["lossy_doc"]
        assert any("text-coverage" in r for r in flagged[0]["reasons"])


class TestRenderConsole:
    """The console summary is emitted to stdout (an explicit deliverable)."""

    def test_summary_lines_reach_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        parsed = tmp_path / "parsed"
        cleaned = tmp_path / "cleaned"
        _write_document_pair(parsed, cleaned, "docA")
        report = build_report(
            [analyze_document("docA", parsed, cleaned, False)], "cfg"
        )

        _render_console(report)

        out = capsys.readouterr().out
        assert "cfg" in out
        assert "1" in out  # document count appears somewhere in the summary


class TestMain:
    """main()'s config handling, exit codes, and report output."""

    def _setup(self, tmp_path: Path, mocker) -> tuple[Path, Path, Path]:
        """Mark tmp_path as an instance and write the document pair + config."""
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        parsed = tmp_path / "data" / "parsed"
        cleaned = tmp_path / "data" / "cleaned"
        _write_document_pair(parsed, cleaned, "docA")
        config_path = tmp_path / "config" / "quality_cfg.toml"
        config_path.parent.mkdir()
        config_path.write_text(
            "[module]\n"
            'source_dir = "data/input"\n'
            "[[module.documents]]\n"
            'file = "docA.pdf"\n'
            'title = "A"\n'
            'collection_path = "cms_iom.pub.a"\n'
            "[parse]\n"
            'parsed_dir = "data/parsed"\n'
            "[clean]\n"
            'cleaned_dir = "data/cleaned"\n',
            encoding="utf-8",
        )
        mocker.patch("ingpipe_file_ingestion.quality_report.setup_entry_logging")
        return config_path, parsed, cleaned

    def _run(self, mocker, config: str, *extra: str) -> None:
        mocker.patch(
            "sys.argv", ["quality_report.py", "--config", config, *extra]
        )
        quality_report_module.main()

    def test_report_run_exits_zero_and_writes_json(
        self, tmp_path: Path, mocker
    ) -> None:
        """A run over a valid corpus exits 0 and writes the JSON report."""
        config_path, _, _ = self._setup(tmp_path, mocker)

        # No SystemExit: the report is not a gate.
        self._run(mocker, str(config_path))

        report_path = (
            tmp_path / "logs" / "ingpipe_file_ingestion" / "quality_report"
            / "quality_cfg.json"
        )
        assert report_path.exists()
        report = _json.loads(report_path.read_text(encoding="utf-8"))
        assert report["documents_total"] == 1

    def test_exits_zero_even_with_findings(self, tmp_path: Path, mocker) -> None:
        """Findings do not change the exit code — a report, not a gate."""
        config_path, _parsed, cleaned = self._setup(tmp_path, mocker)
        # Make the document lossy so the report flags it.
        (cleaned / "docA.json").write_text(
            _json.dumps(
                {
                    "document": {"n_parsed_sections": 1, "binary_hash": 1},
                    "sections": [_full_section(1, "unrelated", 1)],
                }
            ),
            encoding="utf-8",
        )

        # Still no SystemExit despite the flagged document.
        self._run(mocker, str(config_path))

        report_path = (
            tmp_path / "logs" / "ingpipe_file_ingestion" / "quality_report"
            / "quality_cfg.json"
        )
        report = _json.loads(report_path.read_text(encoding="utf-8"))
        assert report["flagged_for_review"]

    def test_section_number_check_flag_passes_through(
        self, tmp_path: Path, mocker
    ) -> None:
        """--section-number-check turns the opt-in heuristic on."""
        config_path, _, _ = self._setup(tmp_path, mocker)

        self._run(mocker, str(config_path), "--section-number-check")

        report_path = (
            tmp_path / "logs" / "ingpipe_file_ingestion" / "quality_report"
            / "quality_cfg.json"
        )
        report = _json.loads(report_path.read_text(encoding="utf-8"))
        assert "section_numbers" in report["documents"][0]

    def test_missing_config_and_malformed_toml_exit_one(
        self, tmp_path: Path, mocker
    ) -> None:
        mocker.patch("ingpipe_file_ingestion.quality_report.setup_entry_logging")
        with pytest.raises(SystemExit) as exc:
            self._run(mocker, str(tmp_path / "absent.toml"))
        assert exc.value.code == 1

        bad = tmp_path / "bad.toml"
        bad.write_text("not valid = toml ][", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            self._run(mocker, str(bad))
        assert exc.value.code == 1

    def test_missing_field_empty_documents_and_missing_dirs_exit_one(
        self, tmp_path: Path, mocker
    ) -> None:
        config_path, parsed, _cleaned = self._setup(tmp_path, mocker)

        # Missing required field.
        no_field = tmp_path / "config" / "no_field.toml"
        no_field.write_text("[module]\n", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            self._run(mocker, str(no_field))
        assert exc.value.code == 1

        # Empty document list.
        empty_docs = tmp_path / "config" / "empty.toml"
        empty_docs.write_text(
            "[module]\ndocuments = []\n[parse]\nparsed_dir = \"data/parsed\"\n"
            "[clean]\ncleaned_dir = \"data/cleaned\"\n",
            encoding="utf-8",
        )
        with pytest.raises(SystemExit) as exc:
            self._run(mocker, str(empty_docs))
        assert exc.value.code == 1

        # Missing parsed directory.
        import shutil
        shutil.rmtree(parsed)
        with pytest.raises(SystemExit) as exc:
            self._run(mocker, str(config_path))
        assert exc.value.code == 1
