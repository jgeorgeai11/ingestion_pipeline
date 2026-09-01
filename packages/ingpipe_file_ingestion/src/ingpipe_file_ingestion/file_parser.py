"""Parse source files with Docling and export to multiple output formats.

Uses Docling's DocumentConverter with layout analysis to convert PDF and Word
(.docx) files. Docling JSON is the authoritative, lossless output format and the
only one the downstream clean step reads. Other formats (markdown/html/yaml)
remain available on request but are not produced by default.

Caller is responsible for logging setup.
"""

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from ingpipe_lib.logconfig import get_logger

if TYPE_CHECKING:
    # Type-only imports: avoid importing Docling at module load time while still
    # giving the helpers precise types for type checking.
    from docling.document_converter import DocumentConverter
    from docling_core.types.doc import DoclingDocument

logger = get_logger(__name__)


class FormatConfig(TypedDict):
    """A FORMAT_CONFIG entry: the save_as method name and file extension.

    Typing ``ext`` as a plain ``str`` (rather than ``str | None``) narrows it to
    a concrete string at the ``mkstemp`` and f-string call sites; ``method`` is
    optional because the ``"text"`` format has no save_as method (it is special-
    cased to ``export_to_text`` before the ``method`` is ever read).
    """

    method: str | None
    ext: str


# Map format names to save_as method names and file extensions
FORMAT_CONFIG: dict[str, FormatConfig] = {
    "markdown": {"method": "save_as_markdown", "ext": ".md"},
    "html": {"method": "save_as_html", "ext": ".html"},
    "json": {"method": "save_as_json", "ext": ".json"},
    "yaml": {"method": "save_as_yaml", "ext": ".yaml"},
    "doctags": {"method": "save_as_doctags", "ext": ".doctags"},
    # text has no save_as method in Docling; handled via export_to_text()
    "text": {"method": None, "ext": ".txt"},
}

# Valid PDF backend names accepted in config. This tuple is the single source
# of truth for the allow-list: parse_files_docling validates against it and
# asserts the name->class dict it builds covers exactly these names, so the two
# cannot silently drift. Names are resolved to backend classes inside
# parse_files_docling so importing this module does not import Docling.
# dlparse (DoclingParseDocumentBackend) is docling's canonical PDF backend;
# dlparse_v2 / dlparse_v4 are deprecated subclasses of it that warn and will
# raise in a future release, so they are intentionally not offered.
VALID_PDF_BACKENDS: tuple[str, ...] = ("pypdfium2", "dlparse")


def parse_files_docling(
    source_dir: str | Path,
    file_paths: list[str],
    output_dir: str | Path,
    output_formats: list[str] | None = None,
    do_ocr: bool = False,
    pdf_backend: str = "dlparse",
    max_pages_per_batch: int = 0,
) -> list[str]:
    """Convert source files with Docling and export to requested formats.

    For each file, converts it using Docling's DocumentConverter and writes
    the requested output format files to output_dir. Each output file is
    written atomically (temp file then rename) so a crash mid-write never
    leaves a truncated file that the skip-if-exists logic would mistake for a
    completed parse. Tracks any conversion or export failures and raises after
    all files are processed so the caller can detect partial failures.

    Atomicity is per output file, not per source file: each format is exported
    via its own temp-then-os.replace, so a multi-format request whose later
    format fails can leave an earlier format's complete output on disk while
    the source file is still recorded as a failure. The ingest.py caller only
    ever requests ["json"] (the clean step's sole input) and its skip sentinel
    checks only that .json output, so a partial multi-format export cannot be
    mistaken for a completed parse there; a direct multi-format caller should
    check every format it requested before treating a file as already parsed.

    Args:
        source_dir: Directory containing input files.
        file_paths: Filenames relative to source_dir.
        output_dir: Directory for output files. Created if it does not exist.
        output_formats: List of format names to export. Defaults to
            ["json"] when None. Must be non-empty.
        do_ocr: Whether Docling should run OCR on the document. Defaults to
            False (born-digital corpus); Docling's own default is True.
        pdf_backend: PDF parsing backend name. One of VALID_PDF_BACKENDS.
            Defaults to "dlparse" (Docling's current default backend,
            DoclingParseDocumentBackend).
        max_pages_per_batch: If > 0, a PDF with more pages than this is parsed in
            consecutive page-range slices of at most this size and stitched back
            with DoclingDocument.concatenate, bounding the docling-parse backend's
            per-page memory. 0 (default) disables batching (single-pass parse).
            Applies to PDFs only; other formats always parse in one pass.

    Returns:
        List of filenames that were converted and exported successfully.

    Raises:
        ValueError: If output_formats is empty, or if any output format or the
            pdf_backend is not supported.
        RuntimeError: If any files failed to convert or export.

    Note:
        Side effect: writes converted files to output_dir, one file per
        (source file, format) combination.
    """
    from docling.backend.docling_parse_backend import DoclingParseDocumentBackend
    from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption, WordFormatOption

    # Resolve backend name to class here (keeps Docling imports lazy). The dict
    # keys must match VALID_PDF_BACKENDS exactly so the allow-list has a single
    # source of truth; assert that here to catch drift if either side changes.
    pdf_backends = {
        "pypdfium2": PyPdfiumDocumentBackend,
        "dlparse": DoclingParseDocumentBackend,
    }
    assert set(pdf_backends) == set(VALID_PDF_BACKENDS), (
        "pdf_backends dict and VALID_PDF_BACKENDS have diverged: "
        f"{sorted(pdf_backends)} vs {sorted(VALID_PDF_BACKENDS)}"
    )
    if pdf_backend not in VALID_PDF_BACKENDS:
        raise ValueError(
            f"Unsupported pdf_backend={pdf_backend!r}. Valid: {list(VALID_PDF_BACKENDS)}"
        )

    source_dir_path = Path(source_dir)
    output_dir_path = Path(output_dir)
    output_dir_path.mkdir(parents=True, exist_ok=True)
    logger.debug(f"Output directory: {output_dir_path}")

    if output_formats is None:
        output_formats = ["json"]

    # An empty list would convert every file but write nothing, then report
    # success; that is almost certainly a caller mistake, so reject it.
    if not output_formats:
        raise ValueError("output_formats must contain at least one format")

    # Validate formats before starting conversion
    invalid = [f for f in output_formats if f not in FORMAT_CONFIG]
    if invalid:
        raise ValueError(f"Unsupported output formats: {invalid}. Valid: {list(FORMAT_CONFIG.keys())}")

    # Table structure and accelerator left at Docling defaults (do_table_structure
    # True, table mode ACCURATE, accelerator auto); only do_ocr is exposed.
    pipeline_options = PdfPipelineOptions(do_ocr=do_ocr)
    converter = DocumentConverter(
        format_options={
            # docling stubs want InputFormat enum keys and concrete backend
            # classes; the string keys and backend registry entries are
            # normalized by docling at runtime (behavior pinned by tests).
            "pdf": PdfFormatOption(  # type: ignore[dict-item]
                pipeline_options=pipeline_options,
                backend=pdf_backends[pdf_backend],  # type: ignore[type-abstract]
            ),
            "docx": WordFormatOption(),  # type: ignore[dict-item]
        }
    )
    logger.debug(f"DocumentConverter initialized (do_ocr={do_ocr}, pdf_backend={pdf_backend})")

    failures: list[str] = []
    successes: list[str] = []

    for file_name in file_paths:
        file_path = source_dir_path / file_name
        if not file_path.exists():
            logger.warning(f"File not found, skipping: {file_path}")
            failures.append(f"{file_name}: file not found")
            continue

        # do_ocr and pdf_backend configure the PDF pipeline only; omit them for
        # other formats (e.g. .docx uses WordFormatOption) to avoid a misleading log.
        pdf_settings = (
            f" (pdf-only: do_ocr={do_ocr}, pdf_backend={pdf_backend})"
            if file_path.suffix.lower() == ".pdf"
            else ""
        )
        logger.info(f"Converting {file_name}{pdf_settings}")
        try:
            doc = _convert_document(converter, file_path, max_pages_per_batch)
        # Deliberate per-file resilience boundary: docling can raise a wide
        # range of types for a bad input, and one file must not abort the
        # batch — the failure is recorded and raised in aggregate below.
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to convert {file_name}: {e}")
            failures.append(f"{file_name}: conversion failed - {e}")
            continue
        else:
            logger.debug(f"Conversion successful for {file_name}")
        stem = file_path.stem

        file_had_export_failure = False
        for fmt in output_formats:
            cfg = FORMAT_CONFIG[fmt]
            out_file = output_dir_path / f"{stem}{cfg['ext']}"

            try:
                _export_atomic(doc, fmt, cfg, out_file, output_dir_path)
            # Same deliberate per-file boundary for the export step.
            except Exception as e:  # noqa: BLE001
                logger.error(f"Failed to export {fmt} for {file_name}: {e}")
                failures.append(f"{file_name}: export {fmt} failed - {e}")
                file_had_export_failure = True
                continue
            else:
                logger.debug(f"Export successful: {fmt} -> {stem}{cfg['ext']}")

            logger.info(f"  Exported {fmt} -> {stem}{cfg['ext']}")

        if not file_had_export_failure:
            successes.append(file_name)
        logger.info(f"Finished {file_name}: {len(output_formats)} format(s)")

    if failures:
        failure_summary = "; ".join(failures)
        raise RuntimeError(
            f"{len(failures)} failure(s) during file processing: {failure_summary}"
        )

    return successes


def _page_range_slices(n_pages: int, batch: int) -> list[tuple[int, int]]:
    """Split a page count into 1-based inclusive (start, end) slices.

    Each slice spans at most ``batch`` pages; the final slice carries the
    remainder. Used to parse a large PDF in bounded page ranges.

    Args:
        n_pages: Total pages in the document (>= 1).
        batch: Maximum pages per slice (> 0).

    Returns:
        Ordered (start, end) page ranges covering 1..n_pages.
    """
    return [
        (start, min(start + batch - 1, n_pages))
        for start in range(1, n_pages + 1, batch)
    ]


def _pdf_page_count(file_path: Path) -> int | None:
    """Return a PDF's page count via pdfplumber, or None if it cannot be read.

    Reads only the page tree (no content extraction), so it is cheap. A read
    failure (corrupt/odd PDF) returns None so the caller falls back to a
    single-pass parse rather than aborting the file.

    Args:
        file_path: Path to the PDF.

    Returns:
        The page count, or None if it could not be determined.
    """
    try:
        import pdfplumber

        with pdfplumber.open(file_path) as pdf:
            return len(pdf.pages)
    except Exception as e:  # noqa: BLE001 — any read failure -> single-pass fallback
        logger.warning(
            f"Could not read page count for {file_path.name} ({e}); parsing in a single pass"
        )
        return None


def _convert_document(
    converter: "DocumentConverter", file_path: Path, max_pages_per_batch: int
) -> "DoclingDocument":
    """Convert a file to a DoclingDocument, batching large PDFs by page range.

    A PDF with more than ``max_pages_per_batch`` pages is parsed in consecutive
    page-range slices (each bounding the docling-parse backend's per-page memory)
    and stitched back with ``DoclingDocument.concatenate``, which reproduces the
    single-pass document. Non-PDF inputs, ``max_pages_per_batch <= 0``, an
    unreadable page count, or a PDF within the threshold all take the single-pass
    path. Any ``convert`` failure (including in a slice) propagates to the caller.

    Args:
        converter: A configured Docling DocumentConverter.
        file_path: Path to the source file.
        max_pages_per_batch: Maximum pages per parse slice; 0 disables batching.

    Returns:
        The converted (and, when batched, concatenated) DoclingDocument.
    """
    if file_path.suffix.lower() != ".pdf" or max_pages_per_batch <= 0:
        return converter.convert(str(file_path)).document

    n_pages = _pdf_page_count(file_path)
    if n_pages is None or n_pages <= max_pages_per_batch:
        return converter.convert(str(file_path)).document

    from docling_core.types.doc import DoclingDocument

    slices = _page_range_slices(n_pages, max_pages_per_batch)
    logger.info(
        f"Parsing {file_path.name} in {len(slices)} page-range batch(es) "
        f"of <= {max_pages_per_batch} pages ({n_pages} pages total)"
    )
    docs: list["DoclingDocument"] = []
    for start, end in slices:
        logger.info(f"  pages {start}-{end}")
        docs.append(converter.convert(str(file_path), page_range=(start, end)).document)
    merged = DoclingDocument.concatenate(docs)
    # concatenate does not carry a single source origin, but the clean step needs
    # origin.binary_hash for provenance. All slices are the same PDF (same hash),
    # so restore the origin from the first slice.
    if docs[0].origin is not None:
        merged.origin = docs[0].origin
    return merged


def _export_atomic(
    doc: "DoclingDocument",
    fmt: str,
    cfg: FormatConfig,
    out_file: Path,
    output_dir_path: Path,
) -> None:
    """Export one format for a document to out_file atomically.

    Writes to a temporary file in output_dir_path (same filesystem as the
    target so os.replace is atomic), then renames it into place. A crash
    mid-write leaves only the temp file, never a truncated final output.

    Args:
        doc: Docling document to export.
        fmt: Format name (a key of FORMAT_CONFIG).
        cfg: FORMAT_CONFIG entry for fmt (method name and extension).
        out_file: Final destination path for the exported file.
        output_dir_path: Directory used for the temp file (must match out_file).

    Raises:
        ValueError: If the configured save_as method is missing on the document.
        Exception: Any error raised by Docling's export or by file I/O.
    """
    # Create the temp file in the output dir so os.replace stays atomic (same
    # filesystem; the suffix does not affect atomicity). A neutral ".tmp" suffix
    # keeps a crash-orphan unambiguous against any glob-based output scan rather
    # than masquerading as a real output (e.g. ".json").
    fd, tmp_name = tempfile.mkstemp(
        dir=str(output_dir_path), prefix=f".{out_file.stem}.", suffix=".tmp"
    )
    os.close(fd)
    tmp_path = Path(tmp_name)

    try:
        if fmt == "text":
            # No save_as_text method; write manually
            text_content = doc.export_to_text()
            tmp_path.write_text(text_content, encoding="utf-8")
        else:
            method = getattr(doc, cfg["method"] or "", None)
            if method is None:
                raise ValueError(
                    f"Export method '{cfg['method']}' not found on document for format '{fmt}'"
                )
            method(tmp_path)
        os.replace(tmp_path, out_file)
    except Exception:
        # Clean up the temp file on any failure so it never lingers
        tmp_path.unlink(missing_ok=True)
        raise
