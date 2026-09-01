"""Parse a Docling JSON document into clean, db-ready sections.

Deserializes the parse step's ``DoclingDocument`` JSON, walks the document body
in reading order, and applies a small set of deterministic, generic cleaning
rules (keyed only on Docling labels/structure, never on document-specific
content) to produce a list of :class:`Section` records.

Cleaning rules (see the activity ``clean-docling-json-to-sections``):
  - Drop furniture (``page_header`` / ``page_footer``).
  - Drop elements whose text is empty/whitespace after ``strip()``.
  - ``section_header`` and the document ``title`` element start a new section
    (their text becomes ``heading_text``); all other text accumulates into the
    current section's ``content_text``.
  - ``table`` elements render inline as markdown at their reading-order
    position; an all-blank table is skipped.
  - Pictures drop the image and keep any caption text as content.
  - Block text is joined with ``\\n\\n``; each element's text is ``strip()``-ed
    with no internal-whitespace collapsing.
  - Content before the first heading becomes a leading section with
    ``heading_text=None``.
  - A unit is pruned only when it has neither a heading nor content;
    heading-only units are kept (``content_text=None``; ``word_count`` then
    reflects the heading text alone).
  - ``page_start`` / ``page_end`` are the min / max ``prov.page_no`` across the
    section's elements (``None`` when no element carries provenance).
  - An element type that matches none of the rules above is a fail-fast error
    *if it carries text*: it raises ``ValueError`` so text that no cleaning rule
    captures can never be silently dropped. A text-less unhandled element (a
    benign structural container, e.g. a ``GroupItem``) is skipped silently.

The output is identity-agnostic: it is a two-key envelope mirroring the two
tables — a ``document`` block (the parsed section count plus the source
``binary_hash`` provenance) and the ``sections`` records. ``collection_path`` /
``title`` are attached later by the load step from config. The ``binary_hash``
is Docling's ``origin.binary_hash`` (the low 64 bits of ``sha256(source
bytes)``), read from the already-loaded parsed document so parse and clean stay
decoupled (the source file is never re-read here).

Caller is responsible for logging setup.
"""

from pathlib import Path
from typing import Any

from docling_core.types.doc import DoclingDocument
from docling_core.types.doc.document import (
    DocItem,
    PictureItem,
    TableItem,
    TextItem,
)
from ingpipe_lib.logconfig import get_logger

from ingpipe_file_ingestion.cleaned_models import CleanedDocument, Document, Section

# Re-export Section so existing `from ingpipe_file_ingestion.docling_section_parser import Section`
# imports keep working now that the model lives in cleaned_models.
__all__ = [
    "CleanedDocument",
    "Document",
    "Section",
    "parse_docling_json",
    "sections_to_record",
]

logger = get_logger(__name__)

# Docling labels for page furniture (running headers/footers). These are dropped
# wherever they appear; the default body walk already excludes the furniture
# content layer, so this is a cheap guard for any that leak into the body.
FURNITURE_LABELS = frozenset({"page_header", "page_footer"})

# Labels that start a new section. The document title and any section header
# both open a section whose heading_text is the element's own text.
HEADING_LABELS = frozenset({"title", "section_header"})

# Caption-labeled text items appear standalone in the body walk but are also
# rendered via their parent picture/table; drop the standalone copy so caption
# text is not double-counted.
DROP_LABELS = frozenset({"caption"})


class _SectionBuilder:
    """Accumulate elements into a single section as the body is walked.

    Collects heading text, content blocks, and page numbers for one section,
    then materializes a :class:`Section` via :meth:`build`.
    """

    def __init__(self, heading_text: str | None) -> None:
        """Initialize an empty builder.

        Args:
            heading_text: The section's heading, or None for the pre-heading
                (leading) section.
        """
        self.heading_text = heading_text
        self._blocks: list[str] = []
        self._pages: list[int] = []

    def add_content(self, text: str) -> None:
        """Append a non-empty content block to the section.

        Args:
            text: Already-stripped, non-empty block text.
        """
        self._blocks.append(text)

    def add_pages(self, page_nos: list[int]) -> None:
        """Record provenance page numbers contributed by an element.

        Args:
            page_nos: Page numbers from one element's provenance.
        """
        self._pages.extend(page_nos)

    def is_empty(self) -> bool:
        """Return True when the section has neither a heading nor content."""
        return self.heading_text is None and not self._blocks

    def build(self, sort_order: int) -> Section:
        """Materialize the accumulated state into a Section.

        Args:
            sort_order: 1-based position to assign to the section.

        Returns:
            The constructed Section. content_text is None when no content blocks
            were added (word_count then reflects the heading alone);
            page_start/page_end are None when no element carried provenance.
        """
        content_text = "\n\n".join(self._blocks) if self._blocks else None
        # word_count covers the whole section: heading text plus content. Table
        # content is stored as markdown, so its delimiter tokens (| and ---) are
        # split() on whitespace and counted here too (consistent with the
        # cleaned_models validator, which recomputes identically).
        word_count = len((self.heading_text or "").split()) + len((content_text or "").split())
        page_start = min(self._pages) if self._pages else None
        page_end = max(self._pages) if self._pages else None
        return Section(
            sort_order=sort_order,
            heading_text=self.heading_text,
            content_text=content_text,
            word_count=word_count,
            page_start=page_start,
            page_end=page_end,
        )


def _page_nos(item: DocItem) -> list[int]:
    """Extract provenance page numbers from a document item.

    Args:
        item: A Docling document item.

    Returns:
        Page numbers from the item's provenance, or an empty list when the item
        has no provenance.
    """
    prov = getattr(item, "prov", None)
    if not prov:
        return []
    return [p.page_no for p in prov]


def _table_to_markdown(item: TableItem, doc: DoclingDocument) -> str | None:
    """Render a table to markdown, preserving the caption when the grid is blank.

    Args:
        item: The table item.
        doc: The owning document (required by Docling's markdown export).

    Returns:
        The table rendered as markdown (which already includes any caption) when
        the grid has non-blank cell text. When every cell is blank but the table
        carries a non-empty caption, the caption text alone (so it is not lost).
        None when the grid is blank and there is no caption.
    """
    grid = item.data.grid if item.data else []
    has_content = any(
        cell.text and cell.text.strip() for row in grid for cell in row
    )
    if not has_content:
        # Symmetry with the picture path: when the grid is dropped, preserve any
        # caption text. Its standalone copy was already dropped via DROP_LABELS,
        # so this is the only place a blank-grid table's caption survives.
        return item.caption_text(doc).strip() or None
    return item.export_to_markdown(doc=doc).strip() or None


def parse_docling_json(json_path: str | Path) -> tuple[list[Section], int]:
    """Parse a Docling JSON file into cleaned sections plus the source hash.

    Deserializes the file into a :class:`DoclingDocument`, reads its source
    provenance (``origin.binary_hash``) from the loaded document, walks the body
    in reading order, applies the deterministic cleaning rules documented at the
    module level, and returns the sections (ordered by a 1-based ``sort_order``)
    together with the source hash.

    The hash is read from the parsed Docling output, NOT from the source file:
    parse and clean stay decoupled and no source-file access happens here.

    Args:
        json_path: Path to the parse step's Docling JSON output.

    Returns:
        A tuple of (sections, binary_hash). ``sections`` is in reading order with
        contiguous 1-based sort_order, and empty when the document has no
        retainable content. ``binary_hash`` is Docling's ``origin.binary_hash``
        (the low 64 bits of ``sha256(source bytes)``, an unsigned 64-bit
        integer). Note: an empty section list is NOT a benign outcome — it is
        rejected downstream by ``step_clean`` / :class:`CleanedDocument`, which
        treats a document with no retainable content as a cleaning failure.

    Raises:
        OSError: If the file cannot be read.
        ValueError: If the file is not a valid Docling JSON document; if the
            parsed document carries no ``origin``/``binary_hash`` (the NOT-NULL
            provenance intent — for file-based parsing origin is always present);
            or if the body contains an element type that no cleaning rule handles
            yet which carries text. The text-bearing-unhandled case is a
            fail-fast guard against silent content loss; text-less unhandled
            elements are skipped instead.
    """
    json_path = Path(json_path)
    try:
        doc = DoclingDocument.load_from_json(json_path)
    except OSError:
        logger.error(f"Could not read Docling JSON: {json_path}")
        raise
    except Exception as e:
        # load_from_json raises pydantic/JSON errors for malformed input; surface
        # them as a ValueError so callers get a single, predictable failure type.
        logger.error(f"Invalid Docling JSON {json_path}: {e}")
        raise ValueError(f"Invalid Docling JSON {json_path}: {e}") from e

    # Source provenance: Docling records origin.binary_hash (low 64 bits of
    # sha256(source bytes)) on the parsed document. Read it here from the already-
    # loaded document so parse and clean stay decoupled (no source-file access). A
    # missing origin/binary_hash is treated as an error per the NOT-NULL intent;
    # for file-based parsing origin is always present.
    origin = getattr(doc, "origin", None)
    binary_hash = getattr(origin, "binary_hash", None) if origin is not None else None
    if binary_hash is None:
        raise ValueError(
            f"Docling JSON {json_path.name} has no origin.binary_hash "
            "(source provenance is required)"
        )

    # The current section being filled. Starts as a pre-heading (heading_text
    # None) builder so any content before the first heading lands in a leading
    # section. It is only emitted if it ends up non-empty (pruning rule).
    builder = _SectionBuilder(heading_text=None)
    sections: list[Section] = []

    def _flush() -> None:
        """Emit the current builder as a section unless it is fully empty."""
        if not builder.is_empty():
            sections.append(builder.build(len(sections) + 1))

    for item, _level in doc.iterate_items():
        label = str(getattr(item, "label", "") or "")

        if label in FURNITURE_LABELS or label in DROP_LABELS:
            continue

        if isinstance(item, TableItem):
            markdown = _table_to_markdown(item, doc)
            if markdown:
                builder.add_content(markdown)
                builder.add_pages(_page_nos(item))
            continue

        if isinstance(item, PictureItem):
            caption = item.caption_text(doc).strip()
            if caption:
                builder.add_content(caption)
                builder.add_pages(_page_nos(item))
            continue

        if label in HEADING_LABELS:
            heading = (getattr(item, "text", "") or "").strip()
            if not heading:
                # A blank heading carries no information; treat it as furniture.
                continue
            # A heading boundary closes the current section and opens a new one.
            _flush()
            builder = _SectionBuilder(heading_text=heading)
            # item is a NodeItem narrowed by its label; _page_nos only
            # reads the optional .prov attribute, present on DocItems.
            builder.add_pages(_page_nos(item))  # type: ignore[arg-type]
            continue

        # Any remaining text-bearing item (text, list_item, code, footnote,
        # reference, formula, ...) accumulates into the current section. An
        # empty-text item is an intentional skip.
        if isinstance(item, TextItem):
            text = (item.text or "").strip()
            if not text:
                continue
            builder.add_content(text)
            builder.add_pages(_page_nos(item))
            continue

        # Fall-through guard: this element matched none of the cleaning rules
        # above. If it carries text, no rule would capture it, so fail fast
        # rather than silently drop content. A text-less unhandled element is a
        # benign structural container (e.g. a GroupItem) and is skipped.
        snippet = (getattr(item, "text", "") or "").strip()
        if snippet:
            raise ValueError(
                f"Unhandled element {type(item).__name__} (label={label!r}) in "
                f"{json_path.name} carries text not captured by any cleaning "
                f"rule: {snippet[:80]!r}"
            )

    _flush()

    logger.info(f"Parsed {len(sections)} sections from {json_path.name}")
    return sections, binary_hash


def sections_to_record(sections: list[Section], binary_hash: int) -> dict[str, Any]:
    """Build the identity-agnostic, db-ready payload for a document.

    Args:
        sections: Cleaned sections for one document.
        binary_hash: The source provenance hash (Docling's
            ``origin.binary_hash``, the low 64 bits of ``sha256(source bytes)``)
            from :func:`parse_docling_json`.

    Returns:
        A dict envelope ``{"document": {"n_parsed_sections": <int>,
        "binary_hash": <int>}, "sections": [<section dict>, ...]}``. The
        ``document`` block carries the parsed section count and the source hash;
        each section dict carries sort_order, heading_text, content_text,
        word_count, page_start, and page_end. collection_path/title are NOT
        included; the load step attaches identity from config.

    Raises:
        pydantic.ValidationError: If the payload violates the cleaned-document
            schema (e.g. an empty section list, non-contiguous sort_order, or a
            count mismatch). An empty list is the most likely trigger when
            ``parse_docling_json`` returns no sections.
    """
    document = CleanedDocument(
        document=Document(n_parsed_sections=len(sections), binary_hash=binary_hash),
        sections=sections,
    )
    return document.model_dump()
