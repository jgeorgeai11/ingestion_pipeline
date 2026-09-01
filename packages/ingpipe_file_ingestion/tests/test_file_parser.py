"""Tests for file_parser module."""

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from docling.backend.docling_parse_backend import DoclingParseDocumentBackend
from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
from ingpipe_file_ingestion.file_parser import (
    _convert_document,
    _page_range_slices,
    _pdf_page_count,
    parse_files_docling,
)


def _make_writing_doc() -> MagicMock:
    """Build a mock Docling document whose save_as_* methods write the file.

    parse_files_docling writes each format to a temp file (passed positionally
    to the save_as_* method) and then renames it into place. The mock must
    therefore actually create the file it is handed, or the subsequent rename
    fails and the file is recorded as a failure.

    Returns:
        A MagicMock document with save_as_markdown/json/html and export_to_text
        wired to create the path they receive.
    """
    doc = MagicMock()
    doc.save_as_markdown.side_effect = lambda p, *a, **k: Path(p).write_text("# md", encoding="utf-8")
    doc.save_as_json.side_effect = lambda p, *a, **k: Path(p).write_text(json.dumps({"ok": True}), encoding="utf-8")
    doc.save_as_html.side_effect = lambda p, *a, **k: Path(p).write_text("<html></html>", encoding="utf-8")
    doc.export_to_text.return_value = "extracted text content"
    return doc


class TestParseFilesDocling:
    """Tests for parse_files_docling function."""

    @patch("docling.backend.docling_parse_backend.DoclingParseDocumentBackend", new_callable=MagicMock)
    @patch("docling.datamodel.pipeline_options.PdfPipelineOptions", new_callable=MagicMock)
    @patch("docling.document_converter.PdfFormatOption", new_callable=MagicMock)
    @patch("docling.document_converter.DocumentConverter")
    def test_single_file_success_returns_filename(
        self,
        mock_converter_cls: MagicMock,
        mock_pdf_option: MagicMock,
        mock_pipeline_options: MagicMock,
        mock_backend: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Single file converts successfully and appears in returned list."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        (source_dir / "report.pdf").write_text("fake pdf")
        output_dir = tmp_path / "output"

        mock_doc = _make_writing_doc()
        mock_converter = mock_converter_cls.return_value
        mock_converter.convert.return_value.document = mock_doc

        result = parse_files_docling(source_dir, ["report.pdf"], output_dir)

        assert result == ["report.pdf"]
        mock_converter.convert.assert_called_once()
        mock_doc.save_as_json.assert_called_once()

    @patch("docling.document_converter.DocumentConverter")
    def test_missing_file_raises_runtime_error(
        self,
        mock_converter_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Missing source file raises RuntimeError with descriptive message."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        output_dir = tmp_path / "output"

        with pytest.raises(RuntimeError, match="file not found"):
            parse_files_docling(source_dir, ["missing.pdf"], output_dir)

    @patch("docling.backend.docling_parse_backend.DoclingParseDocumentBackend", new_callable=MagicMock)
    @patch("docling.datamodel.pipeline_options.PdfPipelineOptions", new_callable=MagicMock)
    @patch("docling.document_converter.PdfFormatOption", new_callable=MagicMock)
    @patch("docling.document_converter.DocumentConverter")
    def test_output_directory_created_if_missing(
        self,
        mock_converter_cls: MagicMock,
        mock_pdf_option: MagicMock,
        mock_pipeline_options: MagicMock,
        mock_backend: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Output directory is created when it does not exist."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        (source_dir / "doc.pdf").write_text("fake pdf")
        output_dir = tmp_path / "nested" / "output"

        mock_doc = _make_writing_doc()
        mock_converter = mock_converter_cls.return_value
        mock_converter.convert.return_value.document = mock_doc

        parse_files_docling(source_dir, ["doc.pdf"], output_dir)

        assert output_dir.exists()

    @patch("docling.backend.docling_parse_backend.DoclingParseDocumentBackend", new_callable=MagicMock)
    @patch("docling.datamodel.pipeline_options.PdfPipelineOptions", new_callable=MagicMock)
    @patch("docling.document_converter.PdfFormatOption", new_callable=MagicMock)
    @patch("docling.document_converter.DocumentConverter")
    def test_multiple_files_one_fails_raises_runtime_error(
        self,
        mock_converter_cls: MagicMock,
        mock_pdf_option: MagicMock,
        mock_pipeline_options: MagicMock,
        mock_backend: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Multiple files where one fails conversion raises RuntimeError."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        (source_dir / "good.pdf").write_text("fake pdf")
        (source_dir / "bad.pdf").write_text("fake pdf")
        output_dir = tmp_path / "output"

        mock_doc = _make_writing_doc()
        mock_converter = mock_converter_cls.return_value
        mock_converter.convert.side_effect = [
            MagicMock(document=mock_doc),
            Exception("conversion error"),
        ]

        with pytest.raises(RuntimeError, match=r"bad\.pdf"):
            parse_files_docling(
                source_dir, ["good.pdf", "bad.pdf"], output_dir
            )

    @patch("docling.backend.docling_parse_backend.DoclingParseDocumentBackend", new_callable=MagicMock)
    @patch("docling.datamodel.pipeline_options.PdfPipelineOptions", new_callable=MagicMock)
    @patch("docling.document_converter.PdfFormatOption", new_callable=MagicMock)
    @patch("docling.document_converter.DocumentConverter")
    def test_default_output_formats_is_json(
        self,
        mock_converter_cls: MagicMock,
        mock_pdf_option: MagicMock,
        mock_pipeline_options: MagicMock,
        mock_backend: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Default output_formats is json when None is passed.

        ingest.main() also defaults to ["json"]; Docling JSON is the only format
        the downstream clean step reads.
        """
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        (source_dir / "doc.pdf").write_text("fake pdf")
        output_dir = tmp_path / "output"

        mock_doc = _make_writing_doc()
        mock_converter = mock_converter_cls.return_value
        mock_converter.convert.return_value.document = mock_doc

        parse_files_docling(source_dir, ["doc.pdf"], output_dir, output_formats=None)

        mock_doc.save_as_json.assert_called_once()
        # Verify no other save_as methods were called
        mock_doc.save_as_html.assert_not_called()
        mock_doc.save_as_markdown.assert_not_called()

    def test_invalid_output_format_raises_value_error(self, tmp_path: Path) -> None:
        """Invalid output format raises ValueError before any conversion."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        output_dir = tmp_path / "output"

        with pytest.raises(ValueError, match="Unsupported output formats"):
            parse_files_docling(
                source_dir, ["doc.pdf"], output_dir, output_formats=["invalid_fmt"]
            )

    @patch("docling.backend.docling_parse_backend.DoclingParseDocumentBackend", new_callable=MagicMock)
    @patch("docling.datamodel.pipeline_options.PdfPipelineOptions", new_callable=MagicMock)
    @patch("docling.document_converter.PdfFormatOption", new_callable=MagicMock)
    @patch("docling.document_converter.DocumentConverter")
    def test_multiple_output_formats_produces_multiple_files(
        self,
        mock_converter_cls: MagicMock,
        mock_pdf_option: MagicMock,
        mock_pipeline_options: MagicMock,
        mock_backend: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Multiple output formats call corresponding save methods."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        (source_dir / "doc.pdf").write_text("fake pdf")
        output_dir = tmp_path / "output"

        mock_doc = _make_writing_doc()
        mock_converter = mock_converter_cls.return_value
        mock_converter.convert.return_value.document = mock_doc

        result = parse_files_docling(
            source_dir,
            ["doc.pdf"],
            output_dir,
            output_formats=["markdown", "html", "json"],
        )

        assert result == ["doc.pdf"]
        mock_doc.save_as_markdown.assert_called_once()
        mock_doc.save_as_html.assert_called_once()
        mock_doc.save_as_json.assert_called_once()

    @patch("docling.backend.docling_parse_backend.DoclingParseDocumentBackend", new_callable=MagicMock)
    @patch("docling.datamodel.pipeline_options.PdfPipelineOptions", new_callable=MagicMock)
    @patch("docling.document_converter.PdfFormatOption", new_callable=MagicMock)
    @patch("docling.document_converter.DocumentConverter")
    def test_text_format_uses_export_to_text(
        self,
        mock_converter_cls: MagicMock,
        mock_pdf_option: MagicMock,
        mock_pipeline_options: MagicMock,
        mock_backend: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Text format calls export_to_text and writes file manually."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        (source_dir / "doc.pdf").write_text("fake pdf")
        output_dir = tmp_path / "output"

        mock_doc = _make_writing_doc()
        mock_converter = mock_converter_cls.return_value
        mock_converter.convert.return_value.document = mock_doc

        result = parse_files_docling(
            source_dir,
            ["doc.pdf"],
            output_dir,
            output_formats=["text"],
        )

        assert result == ["doc.pdf"]
        mock_doc.export_to_text.assert_called_once()
        out_file = output_dir / "doc.txt"
        assert out_file.exists()
        assert out_file.read_text(encoding="utf-8") == "extracted text content"

    @pytest.mark.parametrize("do_ocr", [True, False])
    @patch("docling.backend.docling_parse_backend.DoclingParseDocumentBackend", new_callable=MagicMock)
    @patch("docling.datamodel.pipeline_options.PdfPipelineOptions")
    @patch("docling.document_converter.PdfFormatOption", new_callable=MagicMock)
    @patch("docling.document_converter.DocumentConverter")
    def test_do_ocr_passed_to_pipeline_options(
        self,
        mock_converter_cls: MagicMock,
        mock_pdf_option: MagicMock,
        mock_pipeline_options: MagicMock,
        mock_backend: MagicMock,
        do_ocr: bool,
        tmp_path: Path,
    ) -> None:
        """do_ocr is passed through to PdfPipelineOptions."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        (source_dir / "doc.pdf").write_text("fake pdf")
        output_dir = tmp_path / "output"

        mock_doc = _make_writing_doc()
        mock_converter = mock_converter_cls.return_value
        mock_converter.convert.return_value.document = mock_doc

        parse_files_docling(source_dir, ["doc.pdf"], output_dir, do_ocr=do_ocr)

        mock_pipeline_options.assert_called_once_with(do_ocr=do_ocr)

    @pytest.mark.parametrize(
        ("backend_name", "expected_cls"),
        [
            ("pypdfium2", PyPdfiumDocumentBackend),
            ("dlparse", DoclingParseDocumentBackend),
        ],
    )
    @patch("docling.datamodel.pipeline_options.PdfPipelineOptions", new_callable=MagicMock)
    @patch("docling.document_converter.PdfFormatOption")
    @patch("docling.document_converter.DocumentConverter")
    def test_pdf_backend_maps_to_correct_class(
        self,
        mock_converter_cls: MagicMock,
        mock_pdf_option: MagicMock,
        mock_pipeline_options: MagicMock,
        backend_name: str,
        expected_cls: type,
        tmp_path: Path,
    ) -> None:
        """Each valid pdf_backend string maps to the correct backend class."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        (source_dir / "doc.pdf").write_text("fake pdf")
        output_dir = tmp_path / "output"

        mock_doc = _make_writing_doc()
        mock_converter = mock_converter_cls.return_value
        mock_converter.convert.return_value.document = mock_doc

        parse_files_docling(source_dir, ["doc.pdf"], output_dir, pdf_backend=backend_name)

        mock_pdf_option.assert_called_once()
        assert mock_pdf_option.call_args.kwargs["backend"] is expected_cls

    def test_invalid_pdf_backend_raises_value_error(self, tmp_path: Path) -> None:
        """Invalid pdf_backend raises ValueError before any conversion."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        output_dir = tmp_path / "output"

        with pytest.raises(ValueError, match="Unsupported pdf_backend"):
            parse_files_docling(
                source_dir, ["doc.pdf"], output_dir, pdf_backend="not_a_backend"
            )

    def test_empty_output_formats_raises_value_error(self, tmp_path: Path) -> None:
        """Empty output_formats raises ValueError before any conversion."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        output_dir = tmp_path / "output"

        with pytest.raises(ValueError, match="at least one format"):
            parse_files_docling(
                source_dir, ["doc.pdf"], output_dir, output_formats=[]
            )

    @patch("docling.backend.docling_parse_backend.DoclingParseDocumentBackend", new_callable=MagicMock)
    @patch("docling.datamodel.pipeline_options.PdfPipelineOptions", new_callable=MagicMock)
    @patch("docling.document_converter.WordFormatOption", new_callable=MagicMock)
    @patch("docling.document_converter.PdfFormatOption", new_callable=MagicMock)
    @patch("docling.document_converter.DocumentConverter")
    def test_docx_file_converts_and_omits_pdf_only_log(
        self,
        mock_converter_cls: MagicMock,
        mock_pdf_option: MagicMock,
        mock_word_option: MagicMock,
        mock_pipeline_options: MagicMock,
        mock_backend: MagicMock,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A .docx source converts successfully and its log omits 'pdf-only'.

        Exercises the Word path and the ``else ""`` branch of the pdf-only
        "Converting" log label.
        """
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        (source_dir / "memo.docx").write_text("fake docx")
        output_dir = tmp_path / "output"

        mock_doc = _make_writing_doc()
        mock_converter = mock_converter_cls.return_value
        mock_converter.convert.return_value.document = mock_doc

        with caplog.at_level(logging.INFO, logger="ingpipe_file_ingestion.file_parser"):
            result = parse_files_docling(source_dir, ["memo.docx"], output_dir)

        assert result == ["memo.docx"]
        mock_converter.convert.assert_called_once()
        mock_doc.save_as_json.assert_called_once()
        out_file = output_dir / "memo.json"
        assert out_file.exists()

        converting_lines = [
            r.message for r in caplog.records if r.message.startswith("Converting ")
        ]
        assert len(converting_lines) == 1
        assert all("pdf-only" not in line for line in converting_lines)

    @patch("docling.backend.docling_parse_backend.DoclingParseDocumentBackend", new_callable=MagicMock)
    @patch("docling.datamodel.pipeline_options.PdfPipelineOptions", new_callable=MagicMock)
    @patch("docling.document_converter.PdfFormatOption", new_callable=MagicMock)
    @patch("docling.document_converter.DocumentConverter")
    def test_export_failure_cleans_temp_file_and_raises(
        self,
        mock_converter_cls: MagicMock,
        mock_pdf_option: MagicMock,
        mock_pipeline_options: MagicMock,
        mock_backend: MagicMock,
        tmp_path: Path,
    ) -> None:
        """An export that fails after conversion cleans up its temp file.

        Exercises _export_atomic's except-cleanup branch and the
        file_had_export_failure path: the file is reported as a failure (not in
        successes) and no temp file lingers in output_dir.
        """
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        (source_dir / "doc.pdf").write_text("fake pdf")
        output_dir = tmp_path / "output"

        mock_doc = _make_writing_doc()
        mock_doc.save_as_json.side_effect = RuntimeError("disk full")
        mock_converter = mock_converter_cls.return_value
        mock_converter.convert.return_value.document = mock_doc

        with pytest.raises(RuntimeError, match=r"doc\.pdf"):
            parse_files_docling(
                source_dir, ["doc.pdf"], output_dir, output_formats=["json"]
            )

        # The export failed before any final file was written, so the output
        # dir must be empty: no final output and no temp residue. Asserting on
        # the whole dir (rather than the .<stem>.* temp prefix) avoids coupling
        # the test to the internal temp-file naming convention.
        assert list(output_dir.iterdir()) == []

    @patch("docling.backend.docling_parse_backend.DoclingParseDocumentBackend", new_callable=MagicMock)
    @patch("docling.datamodel.pipeline_options.PdfPipelineOptions", new_callable=MagicMock)
    @patch("docling.document_converter.PdfFormatOption", new_callable=MagicMock)
    @patch("docling.document_converter.DocumentConverter")
    def test_missing_save_as_method_raises_runtime_error_and_cleans_temp(
        self,
        mock_converter_cls: MagicMock,
        mock_pdf_option: MagicMock,
        mock_pipeline_options: MagicMock,
        mock_backend: MagicMock,
        tmp_path: Path,
    ) -> None:
        """A missing configured save_as method raises and leaves no temp file.

        Forces the ``yaml`` format's ``save_as_yaml`` to be absent on the
        document (set to None). _export_atomic's defensive guard raises
        ValueError, which is caught per-format and re-raised as the accumulated
        RuntimeError. Exercises the yaml FORMAT_CONFIG path and the
        "method is None" branch, and confirms no temp file lingers.
        """
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        (source_dir / "doc.pdf").write_text("fake pdf")
        output_dir = tmp_path / "output"

        mock_doc = _make_writing_doc()
        # Force the configured save_as method to be absent so getattr returns
        # None (a bare MagicMock would auto-create the attribute instead).
        mock_doc.save_as_yaml = None
        mock_converter = mock_converter_cls.return_value
        mock_converter.convert.return_value.document = mock_doc

        with pytest.raises(RuntimeError, match=r"doc\.pdf"):
            parse_files_docling(
                source_dir, ["doc.pdf"], output_dir, output_formats=["yaml"]
            )

        # The export failed before any final file was written, so the output
        # dir must be empty: no final output and no temp residue.
        assert list(output_dir.iterdir()) == []

    @patch("docling.backend.docling_parse_backend.DoclingParseDocumentBackend", new_callable=MagicMock)
    @patch("docling.datamodel.pipeline_options.PdfPipelineOptions", new_callable=MagicMock)
    @patch("docling.document_converter.PdfFormatOption", new_callable=MagicMock)
    @patch("docling.document_converter.DocumentConverter")
    def test_json_and_markdown_formats_produce_both_files(
        self,
        mock_converter_cls: MagicMock,
        mock_pdf_option: MagicMock,
        mock_pipeline_options: MagicMock,
        mock_backend: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Requesting ["json","markdown"] writes both a .json and a .md file."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        (source_dir / "doc.pdf").write_text("fake pdf")
        output_dir = tmp_path / "output"

        mock_doc = _make_writing_doc()
        mock_converter = mock_converter_cls.return_value
        mock_converter.convert.return_value.document = mock_doc

        result = parse_files_docling(
            source_dir,
            ["doc.pdf"],
            output_dir,
            output_formats=["json", "markdown"],
        )

        assert result == ["doc.pdf"]
        mock_doc.save_as_json.assert_called_once()
        mock_doc.save_as_markdown.assert_called_once()
        json_file = output_dir / "doc.json"
        md_file = output_dir / "doc.md"
        assert json_file.exists() and json_file.stat().st_size > 0
        assert md_file.exists() and md_file.stat().st_size > 0


class TestPageRangeSlices:
    """_page_range_slices: 1-based inclusive page slicing."""

    def test_exact_multiple(self) -> None:
        assert _page_range_slices(60, 30) == [(1, 30), (31, 60)]

    def test_remainder_tail(self) -> None:
        assert _page_range_slices(70, 30) == [(1, 30), (31, 60), (61, 70)]

    def test_single_page_tail(self) -> None:
        assert _page_range_slices(61, 30) == [(1, 30), (31, 60), (61, 61)]

    def test_fewer_pages_than_batch(self) -> None:
        assert _page_range_slices(10, 30) == [(1, 10)]

    def test_batch_size_one(self) -> None:
        assert _page_range_slices(3, 1) == [(1, 1), (2, 2), (3, 3)]


class TestPdfPageCount:
    """_pdf_page_count: cheap page count with a safe fallback."""

    def test_unreadable_pdf_returns_none(self, tmp_path: Path) -> None:
        """A pdfplumber failure returns None so the caller falls back to single-pass."""
        pdf = tmp_path / "bad.pdf"
        pdf.write_text("not a real pdf")
        with patch("pdfplumber.open", side_effect=Exception("boom")):
            assert _pdf_page_count(pdf) is None

    def test_returns_page_count(self, tmp_path: Path) -> None:
        """A readable PDF returns its page count (len of pdf.pages)."""
        pdf = tmp_path / "ok.pdf"
        pdf.write_text("x")
        fake_pdf = MagicMock()
        fake_pdf.pages = [object(), object(), object()]  # 3 pages
        cm = MagicMock()
        cm.__enter__.return_value = fake_pdf
        cm.__exit__.return_value = False
        with patch("pdfplumber.open", return_value=cm):
            assert _pdf_page_count(pdf) == 3


class TestConvertDocument:
    """_convert_document: single-pass vs page-range batched + concatenate."""

    @staticmethod
    def _converter_returning(doc: MagicMock) -> MagicMock:
        conv = MagicMock()
        conv.convert.return_value.document = doc
        return conv

    def test_non_pdf_parses_single_pass(self, tmp_path: Path) -> None:
        docx = tmp_path / "doc.docx"
        docx.write_text("x")
        doc = MagicMock()
        conv = self._converter_returning(doc)
        out = _convert_document(conv, docx, max_pages_per_batch=30)
        assert out is doc
        conv.convert.assert_called_once_with(str(docx))

    def test_zero_disables_batching_without_reading_pages(self, tmp_path: Path) -> None:
        pdf = tmp_path / "big.pdf"
        pdf.write_text("x")
        doc = MagicMock()
        conv = self._converter_returning(doc)
        with patch("ingpipe_file_ingestion.file_parser._pdf_page_count") as page_count:
            out = _convert_document(conv, pdf, max_pages_per_batch=0)
        assert out is doc
        page_count.assert_not_called()
        conv.convert.assert_called_once_with(str(pdf))

    def test_pdf_within_threshold_parses_single_pass(self, tmp_path: Path) -> None:
        pdf = tmp_path / "small.pdf"
        pdf.write_text("x")
        doc = MagicMock()
        conv = self._converter_returning(doc)
        with patch("ingpipe_file_ingestion.file_parser._pdf_page_count", return_value=10):
            out = _convert_document(conv, pdf, max_pages_per_batch=30)
        assert out is doc
        conv.convert.assert_called_once_with(str(pdf))

    def test_unreadable_page_count_falls_back_single_pass(self, tmp_path: Path) -> None:
        pdf = tmp_path / "weird.pdf"
        pdf.write_text("x")
        doc = MagicMock()
        conv = self._converter_returning(doc)
        with patch("ingpipe_file_ingestion.file_parser._pdf_page_count", return_value=None):
            out = _convert_document(conv, pdf, max_pages_per_batch=30)
        assert out is doc
        conv.convert.assert_called_once_with(str(pdf))

    def test_large_pdf_parses_in_slices_and_concatenates(self, tmp_path: Path) -> None:
        """An over-threshold PDF parses per slice and stitches via concatenate."""
        pdf = tmp_path / "big.pdf"
        pdf.write_text("x")
        slice_docs = [MagicMock(name="d1"), MagicMock(name="d2"), MagicMock(name="d3")]
        conv = MagicMock()
        conv.convert.side_effect = [MagicMock(document=d) for d in slice_docs]
        merged = MagicMock(name="merged")
        with patch("ingpipe_file_ingestion.file_parser._pdf_page_count", return_value=70), patch(
            "docling_core.types.doc.DoclingDocument"
        ) as docling_document:
            docling_document.concatenate.return_value = merged
            out = _convert_document(conv, pdf, max_pages_per_batch=30)
        assert out is merged
        # 70 pages @ 30 -> (1,30), (31,60), (61,70)
        ranges = [c.kwargs["page_range"] for c in conv.convert.call_args_list]
        assert ranges == [(1, 30), (31, 60), (61, 70)]
        docling_document.concatenate.assert_called_once_with(slice_docs)

    def test_pdf_at_exact_threshold_parses_single_pass(self, tmp_path: Path) -> None:
        """A PDF with exactly max_pages_per_batch pages parses single-pass (boundary)."""
        pdf = tmp_path / "exact.pdf"
        pdf.write_text("x")
        doc = MagicMock()
        conv = self._converter_returning(doc)
        with patch("ingpipe_file_ingestion.file_parser._pdf_page_count", return_value=30):
            out = _convert_document(conv, pdf, max_pages_per_batch=30)
        assert out is doc
        conv.convert.assert_called_once_with(str(pdf))

    def test_origin_restored_from_first_slice(self, tmp_path: Path) -> None:
        """The merged doc's origin (binary_hash provenance) is restored from a slice."""
        pdf = tmp_path / "big.pdf"
        pdf.write_text("x")
        slice_docs = [
            SimpleNamespace(origin="SRC_ORIGIN"),
            SimpleNamespace(origin="ignored"),
            SimpleNamespace(origin="ignored"),
        ]
        conv = MagicMock()
        conv.convert.side_effect = [MagicMock(document=d) for d in slice_docs]
        merged = SimpleNamespace(origin=None)
        with patch("ingpipe_file_ingestion.file_parser._pdf_page_count", return_value=70), patch(
            "docling_core.types.doc.DoclingDocument"
        ) as docling_document:
            docling_document.concatenate.return_value = merged
            out = _convert_document(conv, pdf, max_pages_per_batch=30)
        assert out is merged
        assert merged.origin == "SRC_ORIGIN"

    def test_origin_none_left_untouched(self, tmp_path: Path) -> None:
        """If the first slice has no origin, the merged doc's origin is not overwritten."""
        pdf = tmp_path / "big.pdf"
        pdf.write_text("x")
        slice_docs = [SimpleNamespace(origin=None), SimpleNamespace(origin=None)]
        conv = MagicMock()
        conv.convert.side_effect = [MagicMock(document=d) for d in slice_docs]
        merged = SimpleNamespace(origin="UNTOUCHED")
        with patch("ingpipe_file_ingestion.file_parser._pdf_page_count", return_value=40), patch(
            "docling_core.types.doc.DoclingDocument"
        ) as docling_document:
            docling_document.concatenate.return_value = merged
            out = _convert_document(conv, pdf, max_pages_per_batch=30)
        assert out is merged
        assert merged.origin == "UNTOUCHED"

    def test_slice_convert_failure_propagates(self, tmp_path: Path) -> None:
        """A convert failure in any slice propagates (not swallowed) to the caller."""
        pdf = tmp_path / "big.pdf"
        pdf.write_text("x")
        conv = MagicMock()
        conv.convert.side_effect = [
            MagicMock(document=MagicMock()),
            RuntimeError("slice 2 failed"),
        ]
        with patch("ingpipe_file_ingestion.file_parser._pdf_page_count", return_value=40):
            with pytest.raises(RuntimeError, match="slice 2 failed"):
                _convert_document(conv, pdf, max_pages_per_batch=30)
