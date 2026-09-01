"""Tests for the cleaned_models schema (Section and CleanedDocument).

These pin each invariant directly on the schema, independently of the
producer (docling_section_parser) and validator (data_val_cleaned_json) that
share it as the single source of truth.
"""

import pytest
from ingpipe_file_ingestion.cleaned_models import CleanedDocument, Document, Section
from pydantic import ValidationError


def _valid_section(
    sort_order: int = 1,
    heading_text: str | None = "Heading words here",
    content_text: str | None = "Some body content text",
    word_count: int | None = None,
    page_start: int | None = 1,
    page_end: int | None = 2,
) -> Section:
    """Build a self-consistent Section, computing word_count by default.

    Args:
        sort_order: 1-based section position.
        heading_text: Section heading text, or None.
        content_text: Section body text, or None.
        word_count: Explicit count; computed from the text when None.
        page_start: First page (1-based), or None.
        page_end: Last page (1-based), or None.

    Returns:
        A validated Section.
    """
    if word_count is None:
        word_count = len((heading_text or "").split()) + len(
            (content_text or "").split()
        )
    return Section(
        sort_order=sort_order,
        heading_text=heading_text,
        content_text=content_text,
        word_count=word_count,
        page_start=page_start,
        page_end=page_end,
    )


class TestSectionValid:
    """A well-formed Section validates."""

    def test_valid_section_passes(self) -> None:
        """A section with consistent word_count and ordered pages validates."""
        section = _valid_section()

        assert section.word_count == 7
        assert section.page_start == 1
        assert section.page_end == 2

    def test_heading_only_section_passes(self) -> None:
        """A heading-only section (content_text None) validates."""
        section = _valid_section(content_text=None)

        assert section.content_text is None
        assert section.word_count == 3

    def test_content_only_section_passes(self) -> None:
        """A content-only section (heading_text None) validates."""
        section = _valid_section(heading_text=None)

        assert section.heading_text is None
        assert section.word_count == 4

    def test_no_provenance_pages_pass(self) -> None:
        """Both page bounds None (no provenance) validates."""
        section = _valid_section(page_start=None, page_end=None)

        assert section.page_start is None
        assert section.page_end is None


class TestSectionInvariants:
    """Each Section invariant rejects bad input."""

    def test_word_count_mismatch_rejected(self) -> None:
        """A word_count that disagrees with the text is rejected."""
        with pytest.raises(ValidationError, match="word_count is 99, expected 7"):
            _valid_section(word_count=99)

    def test_page_start_after_page_end_rejected(self) -> None:
        """page_start greater than page_end is rejected."""
        with pytest.raises(ValidationError, match=r"page_start .* > page_end"):
            _valid_section(page_start=5, page_end=2)

    def test_fully_empty_section_rejected(self) -> None:
        """A section with neither heading nor content is rejected (2.1)."""
        with pytest.raises(
            ValidationError, match="section has neither heading nor content"
        ):
            _valid_section(heading_text=None, content_text=None, word_count=0)

    def test_page_start_below_one_rejected(self) -> None:
        """A page_start below 1 is rejected by the 1-based bound (2.2)."""
        with pytest.raises(ValidationError, match="page_start"):
            _valid_section(page_start=0, page_end=2)

    def test_page_end_below_one_rejected(self) -> None:
        """A page_end below 1 is rejected by the 1-based bound (2.2)."""
        with pytest.raises(ValidationError, match="page_end"):
            _valid_section(page_start=None, page_end=0)

    def test_negative_word_count_rejected(self) -> None:
        """A negative word_count is rejected by the ge=0 bound."""
        with pytest.raises(ValidationError, match="word_count"):
            _valid_section(word_count=-1)


class TestSectionStrictMode:
    """strict=True keeps bool distinct from int."""

    def test_bool_word_count_rejected(self) -> None:
        """word_count=True is not silently coerced to 1 under strict mode."""
        with pytest.raises(ValidationError, match="word_count"):
            Section(
                sort_order=1,
                heading_text="One",
                content_text=None,
                word_count=True,
                page_start=None,
                page_end=None,
            )

    def test_bool_sort_order_rejected(self) -> None:
        """sort_order=False is not silently coerced to 0 under strict mode."""
        with pytest.raises(ValidationError, match="sort_order"):
            Section(
                sort_order=False,
                heading_text="One",
                content_text=None,
                word_count=1,
                page_start=None,
                page_end=None,
            )


class TestCleanedDocumentValid:
    """A well-formed CleanedDocument validates."""

    def test_valid_document_passes(self) -> None:
        """A document with matching count and contiguous order validates."""
        document = CleanedDocument(
            document=Document(n_parsed_sections=2, binary_hash=12345),
            sections=[_valid_section(sort_order=1), _valid_section(sort_order=2)],
        )

        assert document.document.n_parsed_sections == 2
        assert document.document.binary_hash == 12345
        assert [s.sort_order for s in document.sections] == [1, 2]

    def test_field_order_is_document_then_sections(self) -> None:
        """model_dump emits the document envelope first, then sections.

        The on-disk key order is load-bearing (the clean step dumps this dict as
        JSON), so pin that document precedes sections.
        """
        document = CleanedDocument(
            document=Document(n_parsed_sections=1, binary_hash=7),
            sections=[_valid_section(sort_order=1)],
        )

        dumped = document.model_dump()

        assert list(dumped.keys()) == ["document", "sections"]
        assert list(dumped["document"].keys()) == ["n_parsed_sections", "binary_hash"]


class TestCleanedDocumentInvariants:
    """Each CleanedDocument invariant rejects bad input."""

    def test_count_mismatch_rejected(self) -> None:
        """n_parsed_sections not equal to len(sections) is rejected."""
        with pytest.raises(
            ValidationError, match=r"n_parsed_sections \(2\) != len\(sections\) \(1\)"
        ):
            CleanedDocument(
                document=Document(n_parsed_sections=2, binary_hash=1),
                sections=[_valid_section(sort_order=1)],
            )

    def test_zero_sections_rejected(self) -> None:
        """A zero-section document raises the explicit "zero sections" message.

        The non-empty check runs before the count-equality check, so a payload
        with a schema-valid n_parsed_sections (ge=1) and an empty section list
        fails on the dedicated zero-sections branch rather than as a count
        mismatch.
        """
        with pytest.raises(ValidationError, match="zero sections"):
            CleanedDocument(
                document=Document(n_parsed_sections=1, binary_hash=1), sections=[]
            )

    def test_non_contiguous_sort_order_rejected(self) -> None:
        """A non-1-based-contiguous sort_order sequence is rejected."""
        with pytest.raises(ValidationError, match="sort_order not 1-based contiguous"):
            CleanedDocument(
                document=Document(n_parsed_sections=2, binary_hash=1),
                sections=[_valid_section(sort_order=1), _valid_section(sort_order=3)],
            )

    def test_n_parsed_sections_below_one_rejected(self) -> None:
        """n_parsed_sections below 1 is rejected by the ge=1 bound (1.1)."""
        with pytest.raises(ValidationError, match="n_parsed_sections"):
            CleanedDocument(
                document=Document(n_parsed_sections=0, binary_hash=1),
                sections=[_valid_section()],
            )

    def test_extra_top_level_key_rejected(self) -> None:
        """An unknown top-level key is rejected (extra='forbid')."""
        with pytest.raises(ValidationError, match="surprise"):
            CleanedDocument.model_validate(
                {
                    "document": {"n_parsed_sections": 1, "binary_hash": 1},
                    "sections": [_valid_section(sort_order=1).model_dump()],
                    "surprise": "unexpected",
                }
            )


class TestDocumentSubModel:
    """Invariants on the Document envelope sub-model."""

    def test_valid_document_envelope_passes(self) -> None:
        """A document envelope with a valid count and hash validates."""
        document = Document(n_parsed_sections=3, binary_hash=18446744073709551615)

        assert document.n_parsed_sections == 3
        assert document.binary_hash == 18446744073709551615

    def test_negative_binary_hash_rejected(self) -> None:
        """A negative binary_hash is rejected by the ge=0 bound."""
        with pytest.raises(ValidationError, match="binary_hash"):
            Document(n_parsed_sections=1, binary_hash=-1)

    def test_binary_hash_above_uint64_max_rejected(self) -> None:
        """A binary_hash >= 2**64 is rejected by the le bound (matches the DB
        CHECK and the loaded-docs validator on the unsigned 64-bit range)."""
        with pytest.raises(ValidationError, match="binary_hash"):
            Document(n_parsed_sections=1, binary_hash=18446744073709551616)

    def test_extra_document_key_rejected(self) -> None:
        """An unknown key in the document envelope is rejected (extra='forbid')."""
        with pytest.raises(ValidationError, match="surprise"):
            Document.model_validate(
                {"n_parsed_sections": 1, "binary_hash": 1, "surprise": "x"}
            )

    def test_bool_binary_hash_rejected(self) -> None:
        """binary_hash=True is not silently coerced to 1 under strict mode."""
        with pytest.raises(ValidationError, match="binary_hash"):
            Document(n_parsed_sections=1, binary_hash=True)

    def test_bool_n_parsed_sections_rejected(self) -> None:
        """n_parsed_sections=True is not silently coerced to 1 under strict mode."""
        with pytest.raises(ValidationError, match="n_parsed_sections"):
            Document(n_parsed_sections=True, binary_hash=1)
