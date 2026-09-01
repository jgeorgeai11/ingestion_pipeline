"""Tests for docling_section_parser module.

Fixtures build small DoclingDocument instances programmatically (via the
``add_*`` builders) so each cleaning rule can be exercised in isolation. The
parser is then run against a temp-file JSON dump of the fixture, mirroring how
the pipeline feeds it the parse step's ``.json`` output.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from docling_core.types.doc import DocItemLabel, DoclingDocument
from docling_core.types.doc.base import BoundingBox, CoordOrigin
from docling_core.types.doc.document import (
    DocumentOrigin,
    GraphData,
    ProvenanceItem,
    TableCell,
    TableData,
)
from ingpipe_file_ingestion.docling_section_parser import (
    Section,
    parse_docling_json,
    sections_to_record,
)
from pydantic import ValidationError

# Fixed source hash set on fixture documents so each parse has the origin
# provenance parse_docling_json now requires (it reads origin.binary_hash).
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
        rows: Row-major cell text; the first row is treated as the header.

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


def _parse(doc: DoclingDocument, tmp_path: Path) -> list[Section]:
    """Dump a fixture document to JSON and parse it back into sections.

    Sets a known origin on the document (if it lacks one) so the parser's
    required origin.binary_hash read succeeds; returns only the sections so the
    cleaning-rule assertions stay focused on parsed content.

    Args:
        doc: The fixture document to serialize.
        tmp_path: pytest temp directory for the JSON file.

    Returns:
        The parsed sections.
    """
    if doc.origin is None:
        doc.origin = _origin()
    json_path = tmp_path / "fixture.json"
    doc.save_as_json(json_path)
    sections, _binary_hash = parse_docling_json(json_path)
    return sections


class TestParseErrorPaths:
    """Tests for the parse_docling_json failure contract (OSError / ValueError)."""

    def test_missing_file_raises_os_error(self, tmp_path: Path) -> None:
        """A missing file raises OSError (the read-failure contract)."""
        with pytest.raises(OSError):
            parse_docling_json(tmp_path / "does_not_exist.json")

    @pytest.mark.parametrize(
        "payload",
        [
            "this is not json {{{",
            json.dumps({"foo": "bar"}),
        ],
        ids=["garbage", "valid-json-not-docling"],
    )
    def test_malformed_input_raises_value_error(
        self, tmp_path: Path, payload: str
    ) -> None:
        """Garbage bytes or valid-but-non-Docling JSON raise a wrapped ValueError."""
        bad = tmp_path / "bad.json"
        bad.write_text(payload)

        with pytest.raises(ValueError, match="Invalid Docling JSON"):
            parse_docling_json(bad)

    def test_missing_origin_raises_value_error(self, tmp_path: Path) -> None:
        """A parsed doc with no origin raises (the NOT-NULL provenance intent).

        A DoclingDocument with no origin has origin.binary_hash unavailable, so
        the parser fails fast rather than emitting a record with no provenance.
        """
        json_path = tmp_path / "no_origin.json"
        # No origin set: origin is None on the round-tripped document.
        doc = DoclingDocument(name="t")
        doc.add_text(label=DocItemLabel.TEXT, text="Body.", prov=_prov(1))
        doc.save_as_json(json_path)

        with pytest.raises(ValueError, match=r"no origin\.binary_hash"):
            parse_docling_json(json_path)


class TestSourceHash:
    """Tests that parse_docling_json surfaces the source binary_hash."""

    def test_parse_returns_origin_binary_hash(self, tmp_path: Path) -> None:
        """The parsed document's origin.binary_hash is returned alongside sections."""
        doc = DoclingDocument(name="t", origin=_origin())
        doc.add_heading(text="Heading", level=1, prov=_prov(1))
        doc.add_text(label=DocItemLabel.TEXT, text="Body.", prov=_prov(1))
        json_path = tmp_path / "fixture.json"
        doc.save_as_json(json_path)

        sections, binary_hash = parse_docling_json(json_path)

        assert binary_hash == FIXTURE_BINARY_HASH
        assert len(sections) == 1

    def test_record_carries_origin_binary_hash(self, tmp_path: Path) -> None:
        """The emitted record's document.binary_hash equals origin.binary_hash."""
        doc = DoclingDocument(name="t", origin=_origin())
        doc.add_heading(text="Heading", level=1, prov=_prov(1))
        doc.add_text(label=DocItemLabel.TEXT, text="Body.", prov=_prov(1))
        json_path = tmp_path / "fixture.json"
        doc.save_as_json(json_path)

        sections, binary_hash = parse_docling_json(json_path)
        record = sections_to_record(sections, binary_hash)

        assert record["document"]["binary_hash"] == FIXTURE_BINARY_HASH


class TestDocumentEdgeCases:
    """Tests for whole-document edge cases (empty / furniture-only / no heading)."""

    def test_empty_document_returns_empty(self, tmp_path: Path) -> None:
        """A document with no body items yields no sections."""
        doc = DoclingDocument(name="t")

        assert _parse(doc, tmp_path) == []

    def test_furniture_only_document_returns_empty(self, tmp_path: Path) -> None:
        """A document containing only furniture yields no sections."""
        doc = DoclingDocument(name="t")
        doc.add_text(label=DocItemLabel.PAGE_HEADER, text="Running header", prov=_prov(1))
        doc.add_text(label=DocItemLabel.PAGE_FOOTER, text="Page 1", prov=_prov(1))

        # The sole pre-heading builder has no content and is pruned on flush.
        assert _parse(doc, tmp_path) == []

    def test_no_heading_yields_single_leading_section(self, tmp_path: Path) -> None:
        """Content with no heading at all becomes one heading_text=None section."""
        doc = DoclingDocument(name="t")
        doc.add_text(label=DocItemLabel.TEXT, text="Only body.", prov=_prov(1))

        sections = _parse(doc, tmp_path)

        assert len(sections) == 1
        assert sections[0].heading_text is None
        assert sections[0].content_text == "Only body."


class TestCleaningRules:
    """Tests for the deterministic cleaning rules in parse_docling_json."""

    def test_furniture_dropped(self, tmp_path: Path) -> None:
        """page_header / page_footer elements are dropped."""
        doc = DoclingDocument(name="t")
        doc.add_text(label=DocItemLabel.PAGE_HEADER, text="Running header", prov=_prov(1))
        doc.add_heading(text="Heading", level=1, prov=_prov(1))
        doc.add_text(label=DocItemLabel.TEXT, text="Body.", prov=_prov(1))
        doc.add_text(label=DocItemLabel.PAGE_FOOTER, text="Page 1", prov=_prov(1))

        sections = _parse(doc, tmp_path)

        assert len(sections) == 1
        assert sections[0].heading_text == "Heading"
        assert sections[0].content_text == "Body."

    def test_empty_elements_dropped(self, tmp_path: Path) -> None:
        """Elements that are empty/whitespace after strip are dropped."""
        doc = DoclingDocument(name="t")
        doc.add_heading(text="Heading", level=1, prov=_prov(1))
        doc.add_text(label=DocItemLabel.TEXT, text="   ", prov=_prov(1))
        doc.add_text(label=DocItemLabel.TEXT, text="Real content.", prov=_prov(1))

        sections = _parse(doc, tmp_path)

        assert sections[0].content_text == "Real content."

    def test_table_rendered_inline_as_markdown(self, tmp_path: Path) -> None:
        """A table renders inline as markdown within the current section."""
        doc = DoclingDocument(name="t")
        doc.add_heading(text="Heading", level=1, prov=_prov(2))
        doc.add_table(data=_table_data([["A", "B"], ["1", "2"]]), prov=_prov(2))

        sections = _parse(doc, tmp_path)

        assert len(sections) == 1
        content = sections[0].content_text
        assert content is not None
        # Markdown table: header cells, a separator row, and the body cells.
        assert "A" in content and "B" in content
        assert content.startswith("|")
        assert "---" in content
        assert "1" in content and "2" in content

    def test_all_blank_table_skipped(self, tmp_path: Path) -> None:
        """A table whose cells are all blank is skipped entirely."""
        doc = DoclingDocument(name="t")
        doc.add_heading(text="Heading", level=1, prov=_prov(1))
        doc.add_table(data=_table_data([["", ""], ["  ", ""]]), prov=_prov(1))

        sections = _parse(doc, tmp_path)

        # Heading-only section kept; the blank table contributed no content.
        assert len(sections) == 1
        assert sections[0].content_text is None
        # word_count covers the heading ("Heading") even with no content.
        # word_count == 0 is unreachable from the parser: blank/whitespace
        # headings and content are dropped and a section with neither heading nor
        # content is pruned, so the smallest attainable count is 1 (a one-word
        # heading-only section). Hence no word_count == 0 test exists.
        assert sections[0].word_count == 1

    def test_blank_table_with_caption_keeps_caption(self, tmp_path: Path) -> None:
        """A blank-grid table with a caption keeps the caption text as content.

        The standalone caption item is dropped via DROP_LABELS, so the table path
        is the only place the caption survives (symmetric with the picture path).
        """
        doc = DoclingDocument(name="t")
        doc.add_heading(text="Heading", level=1, prov=_prov(1))
        caption = doc.add_text(
            label=DocItemLabel.CAPTION, text="Table 1. A blank table.", prov=_prov(1)
        )
        doc.add_table(
            data=_table_data([["", ""], ["  ", ""]]), caption=caption, prov=_prov(1)
        )

        sections = _parse(doc, tmp_path)

        assert len(sections) == 1
        # Caption preserved exactly once: not dropped, and not double-counted.
        assert sections[0].content_text == "Table 1. A blank table."

    def test_blank_table_with_caption_in_leading_section(
        self, tmp_path: Path
    ) -> None:
        """A captioned blank table before any heading lands in the leading section."""
        doc = DoclingDocument(name="t")
        caption = doc.add_text(
            label=DocItemLabel.CAPTION, text="Table 1. Pre-heading table.", prov=_prov(1)
        )
        doc.add_table(
            data=_table_data([["", ""]]), caption=caption, prov=_prov(1)
        )

        sections = _parse(doc, tmp_path)

        assert len(sections) == 1
        assert sections[0].heading_text is None
        assert sections[0].content_text == "Table 1. Pre-heading table."

    def test_picture_caption_kept(self, tmp_path: Path) -> None:
        """A picture drops its image but keeps caption text as content."""
        doc = DoclingDocument(name="t")
        doc.add_heading(text="Heading", level=1, prov=_prov(3))
        caption = doc.add_text(
            label=DocItemLabel.CAPTION, text="Figure 1. A diagram.", prov=_prov(3)
        )
        doc.add_picture(caption=caption, prov=_prov(3))

        sections = _parse(doc, tmp_path)

        assert len(sections) == 1
        # Caption appears once (the standalone caption item is dropped; the
        # picture branch contributes the caption text).
        assert sections[0].content_text == "Figure 1. A diagram."

    def test_standalone_caption_dropped(self, tmp_path: Path) -> None:
        """A CAPTION-labeled text item with no parent table/picture is dropped.

        This pins DROP_LABELS independently of the table/picture paths: the
        orphan caption is emitted by iterate_items() with the ``caption`` label
        (verified during development) and must not fall through to the TextItem
        branch. If DROP_LABELS were removed, the caption would accumulate as
        content and content_text would become "Orphan caption.\\n\\nBody."
        """
        doc = DoclingDocument(name="t")
        doc.add_heading(text="Heading", level=1, prov=_prov(1))
        doc.add_text(
            label=DocItemLabel.CAPTION, text="Orphan caption.", prov=_prov(1)
        )
        doc.add_text(label=DocItemLabel.TEXT, text="Body.", prov=_prov(1))

        sections = _parse(doc, tmp_path)

        assert len(sections) == 1
        # Only the body survives; the standalone caption is dropped, not merged.
        assert sections[0].content_text == "Body."

    def test_image_only_picture_yields_heading_only_section(
        self, tmp_path: Path
    ) -> None:
        """A picture with no caption contributes nothing (heading-only section).

        Covers the ``if caption:`` FALSE branch in the picture path: the picture
        drops its image and, lacking caption text, adds no content.
        """
        doc = DoclingDocument(name="t")
        doc.add_heading(text="Heading", level=1, prov=_prov(1))
        doc.add_picture(prov=_prov(1))

        sections = _parse(doc, tmp_path)

        assert len(sections) == 1
        assert sections[0].heading_text == "Heading"
        assert sections[0].content_text is None

    def test_table_before_heading_lands_in_leading_section(
        self, tmp_path: Path
    ) -> None:
        """A non-blank table before any heading lands in the leading section.

        The blank-table-in-leading case is covered by
        test_blank_table_with_caption_in_leading_section; this covers the
        non-blank table content branch in the pre-heading builder, where the
        rendered markdown promotes the leading builder out of "empty" so _flush
        emits a heading_text=None section.
        """
        doc = DoclingDocument(name="t")
        doc.add_table(data=_table_data([["X", "Y"], ["1", "2"]]), prov=_prov(1))

        sections = _parse(doc, tmp_path)

        assert len(sections) == 1
        assert sections[0].heading_text is None
        content = sections[0].content_text
        assert content is not None
        assert "X" in content and "Y" in content

    def test_title_element_becomes_section(self, tmp_path: Path) -> None:
        """The document title element starts a section (as a heading)."""
        doc = DoclingDocument(name="t")
        doc.add_title(text="Document Title", prov=_prov(1))
        doc.add_text(label=DocItemLabel.TEXT, text="Intro.", prov=_prov(1))

        sections = _parse(doc, tmp_path)

        assert sections[0].heading_text == "Document Title"
        assert sections[0].content_text == "Intro."

    def test_pre_heading_content_gets_none_heading(self, tmp_path: Path) -> None:
        """Content before the first heading becomes a heading_text=None section."""
        doc = DoclingDocument(name="t")
        doc.add_text(label=DocItemLabel.TEXT, text="Preamble.", prov=_prov(1))
        doc.add_heading(text="First Heading", level=1, prov=_prov(2))
        doc.add_text(label=DocItemLabel.TEXT, text="Body.", prov=_prov(2))

        sections = _parse(doc, tmp_path)

        assert len(sections) == 2
        assert sections[0].sort_order == 1
        assert sections[0].heading_text is None
        assert sections[0].content_text == "Preamble."
        assert sections[1].heading_text == "First Heading"

    def test_heading_only_section_kept(self, tmp_path: Path) -> None:
        """A heading with no following content is kept with content_text=None."""
        doc = DoclingDocument(name="t")
        doc.add_heading(text="Empty Heading", level=1, prov=_prov(5))
        doc.add_heading(text="Next Heading", level=1, prov=_prov(6))
        doc.add_text(label=DocItemLabel.TEXT, text="Body.", prov=_prov(6))

        sections = _parse(doc, tmp_path)

        assert len(sections) == 2
        assert sections[0].heading_text == "Empty Heading"
        assert sections[0].content_text is None
        # word_count covers the heading ("Empty Heading" = 2 words) with no content.
        assert sections[0].word_count == 2
        assert sections[1].content_text == "Body."

    def test_word_count_sums_heading_and_content(self, tmp_path: Path) -> None:
        """word_count is the sum of heading words and content words."""
        doc = DoclingDocument(name="t")
        doc.add_heading(text="Two Words", level=1, prov=_prov(1))
        doc.add_text(label=DocItemLabel.TEXT, text="Three content words.", prov=_prov(1))

        sections = _parse(doc, tmp_path)

        # 2 heading words + 3 content words.
        assert sections[0].heading_text == "Two Words"
        assert sections[0].content_text == "Three content words."
        assert sections[0].word_count == 5

    def test_section_header_element_becomes_section(self, tmp_path: Path) -> None:
        """A section_header element starts a section (symmetric with title)."""
        doc = DoclingDocument(name="t")
        doc.add_text(
            label=DocItemLabel.SECTION_HEADER, text="A Section", prov=_prov(1)
        )
        doc.add_text(label=DocItemLabel.TEXT, text="Body.", prov=_prov(1))

        sections = _parse(doc, tmp_path)

        assert len(sections) == 1
        assert sections[0].heading_text == "A Section"
        assert sections[0].content_text == "Body."

    def test_blank_heading_skipped(self, tmp_path: Path) -> None:
        """A heading whose text is whitespace-only does not open a section."""
        doc = DoclingDocument(name="t")
        doc.add_text(label=DocItemLabel.SECTION_HEADER, text="   ", prov=_prov(1))
        doc.add_text(label=DocItemLabel.TEXT, text="Body.", prov=_prov(1))

        sections = _parse(doc, tmp_path)

        # The blank heading is skipped; the body lands in a single leading section.
        assert len(sections) == 1
        assert sections[0].heading_text is None
        assert sections[0].content_text == "Body."

    def test_fully_empty_unit_pruned(self, tmp_path: Path) -> None:
        """A unit with neither heading nor content is pruned (no leading section)."""
        doc = DoclingDocument(name="t")
        # Only furniture and whitespace before the first real heading.
        doc.add_text(label=DocItemLabel.PAGE_HEADER, text="hdr", prov=_prov(1))
        doc.add_text(label=DocItemLabel.TEXT, text="   ", prov=_prov(1))
        doc.add_heading(text="Heading", level=1, prov=_prov(1))
        doc.add_text(label=DocItemLabel.TEXT, text="Body.", prov=_prov(1))

        sections = _parse(doc, tmp_path)

        # No leading heading_text=None section: it had no content.
        assert len(sections) == 1
        assert sections[0].heading_text == "Heading"

    def test_page_provenance_min_max(self, tmp_path: Path) -> None:
        """page_start / page_end are the min / max prov.page_no of the section."""
        doc = DoclingDocument(name="t")
        doc.add_heading(text="Heading", level=1, prov=_prov(4))
        doc.add_text(label=DocItemLabel.TEXT, text="On page 7.", prov=_prov(7))
        doc.add_text(label=DocItemLabel.TEXT, text="On page 5.", prov=_prov(5))

        sections = _parse(doc, tmp_path)

        assert sections[0].page_start == 4
        assert sections[0].page_end == 7

    def test_multi_entry_provenance_flattens_min_max(
        self, tmp_path: Path
    ) -> None:
        """page_start / page_end flatten min / max across a single element's prov.

        Unlike test_page_provenance_min_max (one page per element), this puts
        both extremes inside ONE element's provenance list in reverse order
        ([6, 3]) so _page_nos must return >1 page and the section min/max must
        flatten across that list, not just across elements.
        """
        doc = DoclingDocument(name="t")
        item = doc.add_text(
            label=DocItemLabel.SECTION_HEADER, text="Heading", prov=_prov(6)
        )
        # A second provenance entry on an earlier page: the element itself spans
        # pages 6 and 3, so the section's min/max must come from within this list.
        item.prov.append(_prov(3))

        sections = _parse(doc, tmp_path)

        assert len(sections) == 1
        assert sections[0].page_start == 3
        assert sections[0].page_end == 6

    def test_no_provenance_yields_none_pages(self, tmp_path: Path) -> None:
        """Pages are None when no element in the section carries provenance."""
        doc = DoclingDocument(name="t")
        doc.add_heading(text="Heading", level=1)
        doc.add_text(label=DocItemLabel.TEXT, text="Body without prov.")

        sections = _parse(doc, tmp_path)

        assert sections[0].page_start is None
        assert sections[0].page_end is None

    def test_sort_order_one_based_and_contiguous(self, tmp_path: Path) -> None:
        """sort_order is 1-based and contiguous across retained sections.

        Also asserts each section retained its OWN content across the heading
        boundary _flush (the body that preceded the next heading stays with the
        section it belonged to, not leaking into the next).
        """
        doc = DoclingDocument(name="t")
        doc.add_heading(text="A", level=1, prov=_prov(1))
        doc.add_text(label=DocItemLabel.TEXT, text="a.", prov=_prov(1))
        doc.add_heading(text="B", level=1, prov=_prov(2))
        doc.add_text(label=DocItemLabel.TEXT, text="b.", prov=_prov(2))
        doc.add_heading(text="C", level=1, prov=_prov(3))
        doc.add_text(label=DocItemLabel.TEXT, text="c.", prov=_prov(3))

        sections = _parse(doc, tmp_path)

        assert [s.sort_order for s in sections] == [1, 2, 3]
        # Each section kept its own body across the boundary flush.
        assert sections[0].content_text == "a."
        assert sections[1].content_text == "b."
        assert sections[2].content_text == "c."

    def test_blocks_joined_with_double_newline(self, tmp_path: Path) -> None:
        """Content blocks join with \\n\\n and internal whitespace is preserved."""
        doc = DoclingDocument(name="t")
        doc.add_heading(text="Heading", level=1, prov=_prov(1))
        doc.add_text(label=DocItemLabel.TEXT, text="  First  block.  ", prov=_prov(1))
        doc.add_text(label=DocItemLabel.TEXT, text="Second block.", prov=_prov(1))

        sections = _parse(doc, tmp_path)

        # Each block is stripped at its edges, joined with a blank line, and the
        # internal double space inside the first block is preserved.
        assert sections[0].content_text == "First  block.\n\nSecond block."


class TestUnhandledElementGuard:
    """Tests for the fail-fast guard on unhandled element types.

    The guard at the bottom of the iterate_items loop raises ValueError when an
    element matches none of the cleaning rules but carries text, and silently
    skips a text-less unhandled element (a benign structural container).
    """

    def test_unhandled_element_with_text_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unhandled element carrying text raises ValueError (fail-fast).

        No element constructible via the document API is both unhandled by the
        cleaning rules AND carries text (every text-bearing label maps to a
        TextItem subclass). So this tests the parser's branch logic directly: a
        stub item with a ``.text`` value and a label outside the furniture / drop
        / heading sets, injected by patching iterate_items at the class level
        (the parser loads its own DoclingDocument internally, so an external
        instance's method would not be exercised). SimpleNamespace is used rather
        than Mock so ``.text`` is an explicit string, not a truthy Mock.
        """
        json_path = tmp_path / "fixture.json"
        # A valid Docling JSON (with origin so the binary_hash read succeeds)
        # so load_from_json + the origin check pass before iterate_items.
        DoclingDocument(name="t", origin=_origin()).save_as_json(json_path)

        stub = SimpleNamespace(text="leaked text", label="weird_region")
        monkeypatch.setattr(
            DoclingDocument,
            "iterate_items",
            lambda self, *args, **kwargs: iter([(stub, 0)]),
        )

        with pytest.raises(ValueError, match="Unhandled element"):
            parse_docling_json(json_path)

    def test_unhandled_element_without_text_skipped(self, tmp_path: Path) -> None:
        """A text-less unhandled element is skipped and the rest still parses.

        A KeyValueItem is genuinely unhandled by the cleaning rules (it is not a
        TextItem / TableItem / PictureItem, and its ``key_value_region`` label is
        not furniture / drop / heading) and has no ``.text`` attribute, so it
        exercises the no-raise branch of the guard via the real document API. It
        is yielded by iterate_items both before and after the JSON round-trip
        (asserted below), so this genuinely reaches the guard.
        """
        doc = DoclingDocument(name="t", origin=_origin())
        doc.add_heading(text="Heading", level=1, prov=_prov(1))
        doc.add_text(label=DocItemLabel.TEXT, text="Body.", prov=_prov(1))
        # Empty graph: no cells, so the KeyValueItem carries no text.
        doc.add_key_values(graph=GraphData(cells=[], links=[]))

        # Guard against a vacuous test: confirm the KeyValueItem actually reaches
        # the parser (survives the round-trip and is yielded by iterate_items).
        json_path = tmp_path / "fixture.json"
        doc.save_as_json(json_path)
        reloaded = DoclingDocument.load_from_json(json_path)
        yielded = [type(item).__name__ for item, _ in reloaded.iterate_items()]
        assert "KeyValueItem" in yielded

        sections, _binary_hash = parse_docling_json(json_path)

        # The text-less KeyValueItem is skipped; the document still parses.
        assert len(sections) == 1
        assert sections[0].heading_text == "Heading"
        assert sections[0].content_text == "Body."


class TestSectionsToRecord:
    """Tests for the sections_to_record payload builder."""

    def test_record_shape_and_fields(self) -> None:
        """n_parsed_sections matches list length and dicts carry all fields."""
        sections = [
            Section(
                sort_order=1,
                heading_text="H",
                content_text="C",
                # word_count is validated against the text: "H" + "C" = 2 words.
                word_count=2,
                page_start=1,
                page_end=2,
            ),
            Section(
                sort_order=2,
                heading_text="H2",
                content_text=None,
                # heading-only section: "H2" is one word.
                word_count=1,
                page_start=None,
                page_end=None,
            ),
        ]

        record = sections_to_record(sections, FIXTURE_BINARY_HASH)

        # The payload is the two-key envelope: a document block + sections.
        assert list(record.keys()) == ["document", "sections"]
        assert record["document"]["n_parsed_sections"] == 2
        assert record["document"]["binary_hash"] == FIXTURE_BINARY_HASH
        assert list(record["document"].keys()) == ["n_parsed_sections", "binary_hash"]
        assert len(record["sections"]) == 2
        expected_keys = {
            "sort_order",
            "heading_text",
            "content_text",
            "word_count",
            "page_start",
            "page_end",
        }
        for section_dict in record["sections"]:
            assert set(section_dict.keys()) == expected_keys
        # Round-trips cleanly to JSON (the payload the clean step writes).
        assert json.loads(json.dumps(record)) == record

    def test_empty_sections_record_rejected(self) -> None:
        """An empty section list is no longer a valid cleaned document.

        CleanedDocument forbids zero sections; the zero-section path is handled
        upstream by step_clean raising before sections_to_record is called.
        """
        with pytest.raises(ValidationError):
            sections_to_record([], FIXTURE_BINARY_HASH)
