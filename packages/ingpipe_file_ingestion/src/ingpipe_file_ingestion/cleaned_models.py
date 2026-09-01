"""Pydantic schema for the clean step's cleaned-sections JSON.

Single source of truth for the db-ready JSON shape produced by the clean step
and checked by its output validator. The producer
(``docling_section_parser.sections_to_record``) builds a :class:`CleanedDocument`
and dumps it; the validator
(``data_validation.data_val_cleaned_json``) re-validates a file against the same
models. Keeping both sides on one schema means the shape is defined once.

This module imports ``pydantic`` ONLY (not ``docling_core``), so the validator
can depend on it without pulling in the heavy parse-time dependency.

Field declaration order is load-bearing: ``model_dump()`` preserves it, and the
clean step writes that dict as JSON, so the field order here fixes the on-disk
key order.
"""

from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Section(BaseModel):
    """A cleaned section parsed from a Docling document.

    Fields are declared in on-disk JSON key order; ``model_dump()`` preserves
    that order.

    A section must carry a heading or content: ``heading_text`` and
    ``content_text`` are never both None (enforced in ``_check_invariants``),
    matching the producer, which drops fully-empty sections before building.

    Attributes:
        sort_order: Sequential position of the section (1-based, contiguous
            across a document — enforced at the :class:`CleanedDocument` level).
        heading_text: The section's heading text, or None for content that
            appears before the first heading.
        content_text: Joined body text of the section, or None when the section
            is heading-only (a heading with no following content).
        word_count: Number of whitespace-delimited words in the whole section —
            heading_text plus content_text (0 when neither contributes any words:
            both absent, empty, or whitespace-only, since ``"".split()`` and
            ``"   ".split()`` both return ``[]``). Stored (not computed) so a file
            whose persisted count is wrong is caught.
        page_start: Minimum prov.page_no across the section's elements (1-based,
            so >= 1 when present), or None when no element carries provenance.
        page_end: Maximum prov.page_no across the section's elements (1-based,
            so >= 1 when present), or None when no element carries provenance.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    sort_order: int
    heading_text: str | None
    content_text: str | None
    word_count: int = Field(ge=0)
    page_start: Annotated[int, Field(ge=1)] | None
    page_end: Annotated[int, Field(ge=1)] | None

    @model_validator(mode="after")
    def _check_invariants(self) -> Self:
        """Validate the section's internal invariants.

        word_count is validated rather than computed so a stored value that
        disagrees with the text (a corrupted/stale file) is caught here.

        Returns:
            The validated section.

        Raises:
            ValueError: If both heading_text and content_text are None, if
                word_count does not equal the combined heading and content word
                count, or if both pages are non-null and page_start > page_end.
        """
        if self.heading_text is None and self.content_text is None:
            raise ValueError("section has neither heading nor content")
        expected = len((self.heading_text or "").split()) + len(
            (self.content_text or "").split()
        )
        if self.word_count != expected:
            raise ValueError(
                f"word_count is {self.word_count}, expected {expected} "
                "(heading + content words)"
            )
        if (
            self.page_start is not None
            and self.page_end is not None
            and self.page_start > self.page_end
        ):
            raise ValueError(
                f"page_start ({self.page_start}) > page_end ({self.page_end})"
            )
        return self


class Document(BaseModel):
    """Document-level fields for one cleaned document.

    Holds the document-scoped metrics and provenance: the parsed section count
    and the source content hash. This is the ``document`` envelope of the
    cleaned JSON; its keys mirror the ``document`` table columns the load step
    populates (alongside the config-derived collection_path / title).

    Fields are declared in on-disk JSON key order; ``model_dump()`` preserves
    that order.

    Attributes:
        n_parsed_sections: Number of parsed sections; must equal
            ``len(sections)`` (enforced at the :class:`CleanedDocument` level).
        binary_hash: Docling's ``origin.binary_hash`` for the source file — the
            low 64 bits of ``sha256(source bytes)``, an unsigned 64-bit integer.
            It is the source provenance: the version of the source the document
            was parsed from. Bounded to the full unsigned 64-bit range
            ``[0, 2**64)`` (``ge=0`` because it is unsigned; ``le=2**64-1`` so the
            model agrees with the DB CHECK and the loaded-docs validator on the
            valid range — a uint64 whose top half exceeds a signed bigint, hence
            the numeric column in the schema).
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    n_parsed_sections: int = Field(ge=1)
    binary_hash: int = Field(ge=0, le=18446744073709551615)


class CleanedDocument(BaseModel):
    """The identity-agnostic, db-ready payload for one cleaned document.

    The payload is a two-key envelope whose top-level keys mirror the two
    tables: ``document`` (the document-level row) and ``sections`` (the
    document_content rows). collection_path / title are attached later by the
    load step from config.

    Attributes:
        document: Document-level fields (the parsed section count and the source
            ``binary_hash`` provenance). ``document.n_parsed_sections`` must
            equal ``len(sections)``.
        sections: Cleaned sections in reading order, with contiguous 1-based
            sort_order. Non-empty: a zero-section cleaned document means nothing
            was retained, which is treated as a cleaning failure.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    document: Document
    sections: list[Section]

    @model_validator(mode="after")
    def _check_invariants(self) -> Self:
        """Validate document-level invariants across the section list.

        The count invariant spans the envelope: ``document.n_parsed_sections``
        must equal ``len(sections)``.

        Returns:
            The validated document.

        Raises:
            ValueError: If document.n_parsed_sections does not equal
                len(sections), if there are no sections, or if sort_order is not
                1-based contiguous (1..N).
        """
        # Check non-empty FIRST so a zero-section document raises the explicit
        # "has zero sections" rather than failing as a count mismatch. (With
        # n_parsed_sections constrained ge=1, an empty list also violates the
        # count-equality check below, so this branch must run first to be
        # reachable.)
        if not self.sections:
            raise ValueError("has zero sections")
        if self.document.n_parsed_sections != len(self.sections):
            raise ValueError(
                f"n_parsed_sections ({self.document.n_parsed_sections}) != "
                f"len(sections) ({len(self.sections)})"
            )
        actual_orders = [s.sort_order for s in self.sections]
        expected_orders = list(range(1, len(self.sections) + 1))
        if actual_orders != expected_orders:
            raise ValueError(
                f"sort_order not 1-based contiguous (got "
                f"{actual_orders[:5]}{'...' if len(actual_orders) > 5 else ''})"
            )
        return self
