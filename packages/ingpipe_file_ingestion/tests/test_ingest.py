"""Tests for ingest orchestration helpers."""

import importlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from ingpipe_file_ingestion import ingest as ingest_module
from ingpipe_file_ingestion._utils import ensure_schema
from ingpipe_file_ingestion.ingest import (
    _group_files_by_parse_settings,
    filter_valid_documents,
    get_engine,
    main,
    step_clean,
    step_load,
    step_parse,
)
from pytest_mock import MockerFixture
from sqlalchemy import text
from sqlalchemy.engine import Engine

# The four variables get_engine reads.
POSTGRES_VARS = ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD")


class TestFilterValidDocuments:
    """Tests for collection_path validation / document filtering."""

    def test_all_valid_kept_with_paths(self) -> None:
        """Documents with valid collection_paths are kept and mapped."""
        documents = [
            {"file": "a.pdf", "title": "A", "collection_path": "cms_iom.pub.a"},
            {"file": "b.pdf", "title": "B", "collection_path": "cms_iom.pub.b"},
        ]

        kept, collection_paths = filter_valid_documents(documents)

        assert [d["file"] for d in kept] == ["a.pdf", "b.pdf"]
        assert collection_paths == {
            "a.pdf": "cms_iom.pub.a",
            "b.pdf": "cms_iom.pub.b",
        }

    def test_invalid_path_skipped_valid_kept(self) -> None:
        """A document with an invalid collection_path is dropped; valid ones stay."""
        documents = [
            {"file": "good.pdf", "title": "Good", "collection_path": "cms_iom.pub.good"},
            {"file": "bad.pdf", "title": "Bad", "collection_path": "cms_iom.Pub-Bad"},
        ]

        kept, collection_paths = filter_valid_documents(documents)

        assert [d["file"] for d in kept] == ["good.pdf"]
        assert collection_paths == {"good.pdf": "cms_iom.pub.good"}

    def test_missing_path_skipped(self) -> None:
        """A document with no collection_path key is dropped."""
        documents = [
            {"file": "good.pdf", "title": "Good", "collection_path": "cms_iom.pub.good"},
            {"file": "nopath.pdf", "title": "NoPath"},
        ]

        kept, collection_paths = filter_valid_documents(documents)

        assert [d["file"] for d in kept] == ["good.pdf"]
        assert "nopath.pdf" not in collection_paths

    def test_blank_path_skipped(self) -> None:
        """A document with a blank collection_path is dropped."""
        documents = [
            {"file": "good.pdf", "title": "Good", "collection_path": "cms_iom.pub.good"},
            {"file": "blank.pdf", "title": "Blank", "collection_path": "   "},
        ]

        kept, collection_paths = filter_valid_documents(documents)

        assert [d["file"] for d in kept] == ["good.pdf"]
        assert "blank.pdf" not in collection_paths

    def test_duplicate_file_raises(self) -> None:
        """Two entries with the same ``file`` value fail fast naming the file."""
        documents = [
            {"file": "dup.pdf", "title": "First", "collection_path": "cms_iom.pub.a"},
            {"file": "dup.pdf", "title": "Second", "collection_path": "cms_iom.pub.b"},
        ]

        with pytest.raises(ValueError, match=r"Duplicate document file\(s\).*dup\.pdf"):
            filter_valid_documents(documents)

    def test_duplicate_file_raises_even_when_one_path_invalid(self) -> None:
        """A duplicate filename is a config error regardless of path validity.

        The duplicate check runs across ALL input documents before any
        collection_path filtering, so a repeated filename still raises even if
        one copy would otherwise have been dropped for an invalid path.
        """
        documents = [
            {"file": "dup.pdf", "title": "First", "collection_path": "cms_iom.pub.a"},
            {"file": "dup.pdf", "title": "Second", "collection_path": "cms_iom.Bad-Path"},
        ]

        with pytest.raises(ValueError, match=r"Duplicate document file\(s\)"):
            filter_valid_documents(documents)

    def test_duplicate_file_stem_raises(self) -> None:
        """Two filenames differing only by extension collide and fail fast.

        Every parsed/cleaned artifact is named ``Path(file).stem + ".json"``, so
        ``a.pdf`` and ``a.docx`` would both write ``a.json`` — the second parse
        overwriting the first, and both documents loading the same content under
        two different collection_paths. The check keys on the stem and names both
        offending filenames.
        """
        documents = [
            {"file": "a.pdf", "title": "A pdf", "collection_path": "cms_iom.pub.a"},
            {"file": "a.docx", "title": "A docx", "collection_path": "cms_iom.pub.b"},
        ]

        with pytest.raises(
            ValueError, match=r"Duplicate document file\(s\).*a\.docx.*a\.pdf"
        ):
            filter_valid_documents(documents)

    def test_duplicate_collection_path_raises(self) -> None:
        """Two documents sharing one collection_path fail fast naming the path.

        ``collection_path`` is the ``document`` primary key, so without this
        guard the second document would be reported as "already ingested" (and
        counted as a success while never being ingested) or, under overwrite,
        would replace the first — silently dropping one document either way.
        """
        documents = [
            {"file": "a.pdf", "title": "A", "collection_path": "cms_iom.pub.same"},
            {"file": "b.pdf", "title": "B", "collection_path": "cms_iom.pub.same"},
        ]

        with pytest.raises(
            ValueError, match=r"Duplicate collection_path\(s\).*cms_iom\.pub\.same"
        ):
            filter_valid_documents(documents)

    def test_duplicate_collection_path_ignored_when_one_is_skipped(self) -> None:
        """Only kept documents collide: a dropped document cannot duplicate a path.

        The second entry's path is invalid, so it is excluded from the run
        entirely and its (coerced) value is never compared against the survivor.
        """
        documents = [
            {"file": "a.pdf", "title": "A", "collection_path": "cms_iom.pub.same"},
            {"file": "b.pdf", "title": "B", "collection_path": "cms_iom.Pub-Same"},
        ]

        kept, collection_paths = filter_valid_documents(documents)

        assert [d["file"] for d in kept] == ["a.pdf"]
        assert collection_paths == {"a.pdf": "cms_iom.pub.same"}

    def test_all_invalid_returns_empty(self) -> None:
        """When every collection_path is invalid, no documents are kept.

        This empty result is what main()'s fail-fast 4.1 guard keys on.
        """
        documents = [
            {"file": "a.pdf", "title": "A", "collection_path": "cms_iom.Bad-A"},
            {"file": "b.pdf", "title": "B"},
        ]

        kept, collection_paths = filter_valid_documents(documents)

        assert kept == []
        assert collection_paths == {}

    def test_non_string_path_coerced_then_skipped(self) -> None:
        """A non-string collection_path is str()-coerced, validated, then skipped.

        ``filter_valid_documents`` coerces ``raw_path`` with ``str(raw_path)``
        before validation. A negative int coerces to ``"-1"`` whose hyphen is
        not a valid ltree label, so the document is dropped rather than crashing
        on the non-string input.
        """
        documents = [
            {"file": "good.pdf", "title": "Good", "collection_path": "cms_iom.pub.good"},
            {"file": "num.pdf", "title": "Num", "collection_path": -1},
        ]

        kept, collection_paths = filter_valid_documents(documents)

        assert [d["file"] for d in kept] == ["good.pdf"]
        assert "num.pdf" not in collection_paths


class TestGroupFilesByParseSettings:
    """Tests for per-file parse-settings grouping."""

    def test_uniform_settings_single_group(self) -> None:
        """All files sharing settings collapse into one group."""
        files = ["a.pdf", "b.pdf", "c.pdf"]
        do_ocr = {f: False for f in files}
        backend = {f: "dlparse" for f in files}

        groups = _group_files_by_parse_settings(files, do_ocr, backend)

        assert groups == {(False, "dlparse"): ["a.pdf", "b.pdf", "c.pdf"]}

    def test_mixed_do_ocr_splits_groups(self) -> None:
        """A per-file do_ocr override produces a separate group."""
        files = ["a.pdf", "scan.pdf", "c.pdf"]
        do_ocr = {"a.pdf": False, "scan.pdf": True, "c.pdf": False}
        backend = {f: "dlparse" for f in files}

        groups = _group_files_by_parse_settings(files, do_ocr, backend)

        assert groups == {
            (False, "dlparse"): ["a.pdf", "c.pdf"],
            (True, "dlparse"): ["scan.pdf"],
        }

    def test_mixed_backend_splits_groups(self) -> None:
        """A per-file pdf_backend override produces a separate group."""
        files = ["a.pdf", "weird.pdf"]
        do_ocr = {f: False for f in files}
        backend = {"a.pdf": "dlparse", "weird.pdf": "pypdfium2"}

        groups = _group_files_by_parse_settings(files, do_ocr, backend)

        assert groups == {
            (False, "dlparse"): ["a.pdf"],
            (False, "pypdfium2"): ["weird.pdf"],
        }

    def test_group_and_file_order_preserved(self) -> None:
        """Groups and files within them keep first-seen order."""
        files = ["a.pdf", "b.pdf", "c.pdf", "d.pdf"]
        do_ocr = {"a.pdf": True, "b.pdf": False, "c.pdf": True, "d.pdf": False}
        backend = {f: "dlparse" for f in files}

        groups = _group_files_by_parse_settings(files, do_ocr, backend)

        assert list(groups.keys()) == [(True, "dlparse"), (False, "dlparse")]
        assert groups[(True, "dlparse")] == ["a.pdf", "c.pdf"]
        assert groups[(False, "dlparse")] == ["b.pdf", "d.pdf"]

    def test_missing_file_falls_back_to_hard_defaults(self) -> None:
        """Files absent from the maps default to (False, 'dlparse')."""
        groups = _group_files_by_parse_settings(["a.pdf"], {}, {})

        assert groups == {(False, "dlparse"): ["a.pdf"]}


def _write(path: Path, content: str = "x") -> None:
    """Write ``content`` to ``path``, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestStepParse:
    """Tests for step_parse skip-sentinel and group-dispatch orchestration."""

    def test_parsed_json_present_skips_file(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A file with non-empty parsed JSON is not reparsed."""
        parsed_dir = tmp_path / "parsed"
        _write(parsed_dir / "a.json")
        mock_parse = mocker.patch("ingpipe_file_ingestion.ingest.parse_files_docling")

        step_parse(
            source_dir=str(tmp_path / "src"),
            file_paths=["a.pdf"],
            parsed_dir=str(parsed_dir),
            overwrite=False,
        )

        mock_parse.assert_not_called()

    def test_empty_output_reparses_file(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A zero-byte output file is treated as absent and reparsed."""
        parsed_dir = tmp_path / "parsed"
        _write(parsed_dir / "a.json", content="")
        mock_parse = mocker.patch("ingpipe_file_ingestion.ingest.parse_files_docling")

        ok_files, failures = step_parse(
            source_dir=str(tmp_path / "src"),
            file_paths=["a.pdf"],
            parsed_dir=str(parsed_dir),
            overwrite=False,
        )

        mock_parse.assert_called_once()
        # The mock writes nothing, so the empty .json is still unusable after the
        # reparse attempt -> a.pdf is a failure (no group error -> default reason).
        assert ok_files == []
        assert failures == [
            {
                "file": "a.pdf",
                "stage": "parse",
                "reason": "parsed JSON output missing or empty",
            }
        ]

    def test_overwrite_bypasses_skip_sentinel(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """overwrite=True reparses even when parsed JSON already exists."""
        parsed_dir = tmp_path / "parsed"
        _write(parsed_dir / "a.json")
        mock_parse = mocker.patch("ingpipe_file_ingestion.ingest.parse_files_docling")

        step_parse(
            source_dir=str(tmp_path / "src"),
            file_paths=["a.pdf"],
            parsed_dir=str(parsed_dir),
            overwrite=True,
        )

        mock_parse.assert_called_once()
        assert mock_parse.call_args.args[1] == ["a.pdf"]

    def test_overwrite_deletes_stale_json_so_failed_reparse_is_recorded(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """Under overwrite, a stale .json is deleted before re-converting.

        So a re-conversion that fails (and writes nothing) is recorded as a
        failure rather than masked as a survivor by the leftover JSON.
        """
        parsed_dir = tmp_path / "parsed"
        _write(parsed_dir / "a.json", content="{}")  # stale, from a prior run
        # Re-conversion fails (raises) and writes no fresh JSON.
        mocker.patch(
            "ingpipe_file_ingestion.ingest.parse_files_docling",
            side_effect=RuntimeError("convert failed"),
        )

        ok_files, failures = step_parse(
            source_dir=str(tmp_path / "src"),
            file_paths=["a.pdf"],
            parsed_dir=str(parsed_dir),
            overwrite=True,
        )

        # The stale JSON was deleted before the failed re-convert, so a.pdf is a
        # recorded failure (with its group's reason), not a silent survivor.
        assert ok_files == []
        assert failures == [
            {"file": "a.pdf", "stage": "parse", "reason": "convert failed"}
        ]
        assert not (parsed_dir / "a.json").exists()

    def test_mixed_batch_dispatches_one_call_per_group(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A mixed batch issues one parse call per (do_ocr, pdf_backend) group."""
        mock_parse = mocker.patch("ingpipe_file_ingestion.ingest.parse_files_docling")

        step_parse(
            source_dir=str(tmp_path / "src"),
            file_paths=["a.pdf", "scan.pdf", "c.pdf"],
            parsed_dir=str(tmp_path / "parsed"),
            overwrite=True,
            do_ocr_map={"a.pdf": False, "scan.pdf": True, "c.pdf": False},
            pdf_backend_map={f: "dlparse" for f in ["a.pdf", "scan.pdf", "c.pdf"]},
        )

        assert mock_parse.call_count == 2
        dispatched = {
            (call.kwargs["do_ocr"], call.kwargs["pdf_backend"]): call.args[1]
            for call in mock_parse.call_args_list
        }
        assert dispatched == {
            (False, "dlparse"): ["a.pdf", "c.pdf"],
            (True, "dlparse"): ["scan.pdf"],
        }

    def test_max_pages_per_batch_forwarded_to_parse(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """step_parse forwards max_pages_per_batch through to parse_files_docling."""
        mock_parse = mocker.patch("ingpipe_file_ingestion.ingest.parse_files_docling")

        step_parse(
            source_dir=str(tmp_path / "src"),
            file_paths=["a.pdf"],
            parsed_dir=str(tmp_path / "parsed"),
            overwrite=True,
            do_ocr_map={"a.pdf": False},
            pdf_backend_map={"a.pdf": "dlparse"},
            max_pages_per_batch=25,
        )

        assert mock_parse.call_args.kwargs["max_pages_per_batch"] == 25

    def test_max_pages_per_batch_defaults_to_zero(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """When unset, step_parse forwards max_pages_per_batch=0 (batching off)."""
        mock_parse = mocker.patch("ingpipe_file_ingestion.ingest.parse_files_docling")

        step_parse(
            source_dir=str(tmp_path / "src"),
            file_paths=["a.pdf"],
            parsed_dir=str(tmp_path / "parsed"),
            overwrite=True,
            do_ocr_map={"a.pdf": False},
            pdf_backend_map={"a.pdf": "dlparse"},
        )

        assert mock_parse.call_args.kwargs["max_pages_per_batch"] == 0

    def test_group_failure_records_failure_and_continues(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A RuntimeError in one group is recorded, not raised; later groups run.

        Survivors are read from the parsed JSON on disk. The surviving group's
        mock writes its JSON so that file lands in ``ok_files``; the failing
        group writes nothing so its file lands in ``failures`` with the group's
        RuntimeError message as the reason. The run does NOT abort.
        """
        parsed_dir = tmp_path / "parsed"

        def fake_parse(
            source_dir: str,
            files: list[str],
            out_dir: str,
            formats: list[str],
            *,
            do_ocr: bool,
            pdf_backend: str,
            max_pages_per_batch: int = 0,
        ) -> None:
            # The OCR group (scan.pdf) fails; the non-OCR group (a.pdf) writes
            # its parsed JSON so it survives.
            if do_ocr:
                raise RuntimeError("group scan failed")
            for f in files:
                _write(parsed_dir / (Path(f).stem + ".json"), content="{}")

        mock_parse = mocker.patch(
            "ingpipe_file_ingestion.ingest.parse_files_docling", side_effect=fake_parse
        )

        ok_files, failures = step_parse(
            source_dir=str(tmp_path / "src"),
            file_paths=["a.pdf", "scan.pdf"],
            parsed_dir=str(parsed_dir),
            overwrite=True,
            do_ocr_map={"a.pdf": False, "scan.pdf": True},
        )

        # Both groups attempted despite the first one failing (no abort).
        assert mock_parse.call_count == 2
        # The good file survived; the failing file is recorded with the reason.
        assert ok_files == ["a.pdf"]
        assert failures == [
            {"file": "scan.pdf", "stage": "parse", "reason": "group scan failed"}
        ]

    def test_all_groups_succeed_returns_all_ok(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """When every file's JSON is written, all are survivors and no failures."""
        parsed_dir = tmp_path / "parsed"

        def fake_parse(
            source_dir: str,
            files: list[str],
            out_dir: str,
            formats: list[str],
            *,
            do_ocr: bool,
            pdf_backend: str,
            max_pages_per_batch: int = 0,
        ) -> None:
            for f in files:
                _write(parsed_dir / (Path(f).stem + ".json"), content="{}")

        mocker.patch("ingpipe_file_ingestion.ingest.parse_files_docling", side_effect=fake_parse)

        ok_files, failures = step_parse(
            source_dir=str(tmp_path / "src"),
            file_paths=["a.pdf", "b.pdf"],
            parsed_dir=str(parsed_dir),
            overwrite=True,
        )

        assert ok_files == ["a.pdf", "b.pdf"]
        assert failures == []

    def test_already_parsed_file_counts_as_ok(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A skipped (already-parsed) file is a survivor without reparsing."""
        parsed_dir = tmp_path / "parsed"
        _write(parsed_dir / "a.json")
        mock_parse = mocker.patch("ingpipe_file_ingestion.ingest.parse_files_docling")

        ok_files, failures = step_parse(
            source_dir=str(tmp_path / "src"),
            file_paths=["a.pdf"],
            parsed_dir=str(parsed_dir),
            overwrite=False,
        )

        mock_parse.assert_not_called()
        assert ok_files == ["a.pdf"]
        assert failures == []

    def test_value_error_propagates_immediately(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A ValueError (invalid pdf_backend) is not accumulated; it escapes."""
        mock_parse = mocker.patch(
            "ingpipe_file_ingestion.ingest.parse_files_docling",
            side_effect=ValueError("bad pdf_backend"),
        )

        with pytest.raises(ValueError, match="bad pdf_backend"):
            step_parse(
                source_dir=str(tmp_path / "src"),
                file_paths=["a.pdf", "scan.pdf"],
                parsed_dir=str(tmp_path / "parsed"),
                overwrite=True,
                do_ocr_map={"a.pdf": False, "scan.pdf": True},
            )

        # Raised on the first group; the second group is never attempted.
        assert mock_parse.call_count == 1


class TestStepClean:
    """Tests for step_clean read-parse-write orchestration and failure policy."""

    def test_happy_path_writes_cleaned_json(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A parsed JSON with sections is cleaned and written to cleaned_dir."""
        parsed_dir = tmp_path / "parsed"
        cleaned_dir = tmp_path / "cleaned"
        _write(parsed_dir / "a.json", content="{}")
        # parse_docling_json now returns (sections, binary_hash).
        mock_parse = mocker.patch(
            "ingpipe_file_ingestion.ingest.parse_docling_json", return_value=(["sec1", "sec2"], 42)
        )
        mock_to_record = mocker.patch(
            "ingpipe_file_ingestion.ingest.sections_to_record",
            return_value={
                "document": {"n_parsed_sections": 2, "binary_hash": 42},
                "sections": [],
            },
        )

        ok_files, failures = step_clean(
            parsed_dir=str(parsed_dir),
            cleaned_dir=str(cleaned_dir),
            file_paths=["a.pdf"],
            overwrite=False,
        )

        mock_parse.assert_called_once()
        # The hash from parse_docling_json is threaded into sections_to_record.
        mock_to_record.assert_called_once_with(["sec1", "sec2"], 42)
        out_path = cleaned_dir / "a.json"
        assert out_path.exists()
        assert out_path.stat().st_size > 0
        assert ok_files == ["a.pdf"]
        assert failures == []

    def test_missing_parsed_json_records_failure(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A missing parsed source is recorded as a failure, not raised."""
        parsed_dir = tmp_path / "parsed"
        cleaned_dir = tmp_path / "cleaned"
        parsed_dir.mkdir()  # exists but holds no a.json
        mock_parse = mocker.patch("ingpipe_file_ingestion.ingest.parse_docling_json")

        ok_files, failures = step_clean(
            parsed_dir=str(parsed_dir),
            cleaned_dir=str(cleaned_dir),
            file_paths=["a.pdf"],
            overwrite=False,
        )

        mock_parse.assert_not_called()
        assert not (cleaned_dir / "a.json").exists()
        assert ok_files == []
        assert len(failures) == 1
        assert failures[0]["file"] == "a.pdf"
        assert failures[0]["stage"] == "clean"
        assert "a.json" in failures[0]["reason"]

    def test_zero_section_parse_records_failure_and_writes_nothing(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """An empty parse is recorded as a failure and persists no output."""
        parsed_dir = tmp_path / "parsed"
        cleaned_dir = tmp_path / "cleaned"
        _write(parsed_dir / "a.json", content="{}")
        # Zero sections with a hash: the (sections, binary_hash) tuple must be
        # unpacked so the `if not sections:` zero-section guard still fires.
        mocker.patch("ingpipe_file_ingestion.ingest.parse_docling_json", return_value=([], 42))
        mock_to_record = mocker.patch("ingpipe_file_ingestion.ingest.sections_to_record")

        ok_files, failures = step_clean(
            parsed_dir=str(parsed_dir),
            cleaned_dir=str(cleaned_dir),
            file_paths=["a.pdf"],
            overwrite=False,
        )

        # No empty record is built or written for a zero-section parse.
        mock_to_record.assert_not_called()
        assert not (cleaned_dir / "a.json").exists()
        # Pin "writes nothing" literally: the cleaned dir has no JSON output at all.
        assert list(cleaned_dir.glob("*.json")) == []
        assert ok_files == []
        assert len(failures) == 1
        assert failures[0]["stage"] == "clean"
        assert "No sections parsed" in failures[0]["reason"]

    def test_mixed_batch_drops_bad_keeps_good(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """One file failing to clean does not abort the batch; the good one survives.

        The first file is missing its parsed JSON (recorded as a failure); the
        second has a valid parse and is cleaned. The loop must process both.
        """
        parsed_dir = tmp_path / "parsed"
        cleaned_dir = tmp_path / "cleaned"
        parsed_dir.mkdir()
        _write(parsed_dir / "good.json", content="{}")  # bad.json deliberately absent
        mocker.patch(
            "ingpipe_file_ingestion.ingest.parse_docling_json", return_value=(["sec1"], 42)
        )
        mocker.patch(
            "ingpipe_file_ingestion.ingest.sections_to_record",
            return_value={
                "document": {"n_parsed_sections": 1, "binary_hash": 42},
                "sections": [],
            },
        )

        ok_files, failures = step_clean(
            parsed_dir=str(parsed_dir),
            cleaned_dir=str(cleaned_dir),
            file_paths=["bad.pdf", "good.pdf"],
            overwrite=False,
        )

        # The good file was cleaned despite the bad file failing first.
        assert ok_files == ["good.pdf"]
        assert (cleaned_dir / "good.json").exists()
        assert len(failures) == 1
        assert failures[0]["file"] == "bad.pdf"
        assert failures[0]["stage"] == "clean"

    def test_failed_write_leaves_no_partial_or_temp_output(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """An interrupted write leaves no cleaned output and no temp file behind.

        The cleaned JSON is written to a temp file and renamed into place, so a
        crash or disk-full mid-write can never leave a truncated ``<stem>.json``
        that the next non-overwrite run would skip as complete. The rename is
        made to fail here; the orphan temp file must be cleaned up and the file
        recorded as a per-file clean failure.
        """
        parsed_dir = tmp_path / "parsed"
        cleaned_dir = tmp_path / "cleaned"
        _write(parsed_dir / "a.json", content="{}")
        mocker.patch(
            "ingpipe_file_ingestion.ingest.parse_docling_json", return_value=(["sec1"], 42)
        )
        mocker.patch(
            "ingpipe_file_ingestion.ingest.sections_to_record",
            return_value={
                "document": {"n_parsed_sections": 1, "binary_hash": 42},
                "sections": [],
            },
        )
        mocker.patch("ingpipe_file_ingestion.ingest.os.replace", side_effect=OSError("disk full"))

        ok_files, failures = step_clean(
            parsed_dir=str(parsed_dir),
            cleaned_dir=str(cleaned_dir),
            file_paths=["a.pdf"],
            overwrite=False,
        )

        # No output at the final path, and no leftover temp file in the directory.
        assert not (cleaned_dir / "a.json").exists()
        assert list(cleaned_dir.iterdir()) == []
        assert ok_files == []
        assert len(failures) == 1
        assert failures[0]["file"] == "a.pdf"
        assert failures[0]["stage"] == "clean"
        assert "disk full" in failures[0]["reason"]

    def test_existing_nonempty_output_is_skipped(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A non-empty cleaned output already present is not re-cleaned.

        The source JSON is deliberately absent to pin the idempotent ordering:
        the skip check runs before the source-existence check, so an existing
        output makes the source legitimately unnecessary.
        """
        parsed_dir = tmp_path / "parsed"
        cleaned_dir = tmp_path / "cleaned"
        _write(
            cleaned_dir / "a.json",
            content='{"document": {"n_parsed_sections": 1, "binary_hash": 1}}',
        )
        mock_parse = mocker.patch("ingpipe_file_ingestion.ingest.parse_docling_json")

        step_clean(
            parsed_dir=str(parsed_dir),
            cleaned_dir=str(cleaned_dir),
            file_paths=["a.pdf"],
            overwrite=False,
        )

        mock_parse.assert_not_called()

    def test_overwrite_rewrites_existing_output(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """overwrite=True re-cleans even when a non-empty output exists."""
        parsed_dir = tmp_path / "parsed"
        cleaned_dir = tmp_path / "cleaned"
        _write(parsed_dir / "a.json", content="{}")
        _write(cleaned_dir / "a.json", content='{"stale": true}')
        mock_parse = mocker.patch(
            "ingpipe_file_ingestion.ingest.parse_docling_json", return_value=(["sec1"], 42)
        )
        mocker.patch(
            "ingpipe_file_ingestion.ingest.sections_to_record",
            return_value={
                "document": {"n_parsed_sections": 1, "binary_hash": 42},
                "sections": [],
            },
        )

        step_clean(
            parsed_dir=str(parsed_dir),
            cleaned_dir=str(cleaned_dir),
            file_paths=["a.pdf"],
            overwrite=True,
        )

        mock_parse.assert_called_once()
        assert "stale" not in (cleaned_dir / "a.json").read_text(encoding="utf-8")


# A fixed source hash used by the cleaned-document fixtures so the load step's
# source_binary_hash insert can be asserted against a known value.
FIXTURE_BINARY_HASH = 12345678901234567890


def _cleaned_doc(
    n: int = 2, binary_hash: int = FIXTURE_BINARY_HASH
) -> dict[str, object]:
    """Build a valid cleaned-document payload with ``n`` contiguous sections.

    The payload is the two-key envelope: a ``document`` block (carrying
    ``n_parsed_sections`` and the source ``binary_hash``) and ``sections``.
    """
    return {
        "document": {"n_parsed_sections": n, "binary_hash": binary_hash},
        "sections": [
            {
                "sort_order": i,
                "heading_text": f"Heading {i}",
                "content_text": f"body {i}",
                # "Heading {i}" is 2 words + "body {i}" is 2 words = 4.
                "word_count": 4,
                "page_start": i,
                "page_end": i,
            }
            for i in range(1, n + 1)
        ],
    }


def _write_cleaned(cleaned_dir: Path, stem: str, payload: dict[str, object]) -> Path:
    """Write a cleaned JSON payload to ``cleaned_dir/<stem>.json``."""
    cleaned_dir.mkdir(parents=True, exist_ok=True)
    path = cleaned_dir / f"{stem}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _ensure_document_tables(engine: "Engine", schema: str) -> None:
    """Create the document/document_content tables in an ephemeral schema.

    Runs the REAL schema.sql template through ensure_schema, so the tests
    exercise the exact DDL (types, CHECK constraints, FK cascade) production
    runs.
    """
    ddl_path = Path(ingest_module.__file__).parent / "sql" / "schema.sql"
    ensure_schema(engine, schema, ddl_path)


def _table_counts(engine: "Engine", schema: str) -> tuple[int, int]:
    """Return (document rows, document_content rows) in the schema."""
    with engine.connect() as conn:
        n_docs = conn.execute(
            text(f"select count(*) from {schema}.document")
        ).scalar_one()
        n_content = conn.execute(
            text(f"select count(*) from {schema}.document_content")
        ).scalar_one()
    return n_docs, n_content


def _doc_row(engine: "Engine", schema: str, cp: str) -> dict | None:
    """Fetch the document row for a collection_path, or None."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                f"select collection_path::text as cp, title, n_parsed_sections, "
                f"source_binary_hash from {schema}.document "
                "where collection_path = :cp"
            ),
            {"cp": cp},
        ).fetchone()
    return dict(row._mapping) if row is not None else None


def _content_rows(engine: "Engine", schema: str, cp: str) -> list[dict]:
    """Fetch content rows for a collection_path ordered by sort_order."""
    with engine.connect() as conn:
        result = conn.execute(
            text(
                f"select collection_path::text as cp, sort_order, heading_text, "
                f"content_text, word_count, page_start, page_end "
                f"from {schema}.document_content "
                "where collection_path = :cp order by sort_order"
            ),
            {"cp": cp},
        )
        return [dict(r._mapping) for r in result]


class TestStepLoad:
    """Tests for step_load against a real PostgreSQL ephemeral schema.

    The DB-touching assertions run real SQL (loader statements executed by
    PostgreSQL, results asserted on table state); the pure-logic per-file
    failure paths that never open a transaction keep a bare MagicMock engine.
    """

    def test_inserts_document_and_sections_with_collection_path(
        self, ephemeral_schema: tuple["Engine", str], tmp_path: Path
    ) -> None:
        """A new document lands one doc row plus one content row per section."""
        engine, schema = ephemeral_schema
        _ensure_document_tables(engine, schema)
        cleaned_dir = tmp_path / "cleaned"
        _write_cleaned(cleaned_dir, "ge101c01", _cleaned_doc(n=3))
        cp = "cms_iom.pub_100_01.ge101c01"

        ok_files, failures = step_load(
            cleaned_dir=str(cleaned_dir),
            file_paths=["ge101c01.pdf"],
            engine=engine,
            db_schema=schema,
            overwrite=False,
            collection_paths={"ge101c01.pdf": cp},
            document_titles={"ge101c01.pdf": "Chapter 1"},
        )

        assert ok_files == ["ge101c01.pdf"]
        assert failures == []
        # The stored document row carries the identity, title, section count,
        # and the uint64 source provenance hash (numeric(20,0) -> Decimal).
        doc = _doc_row(engine, schema, cp)
        assert doc is not None
        assert doc["cp"] == cp
        assert doc["title"] == "Chapter 1"
        assert doc["n_parsed_sections"] == 3
        assert int(doc["source_binary_hash"]) == FIXTURE_BINARY_HASH
        # Content rows keyed by the same collection_path, 1-based contiguous.
        content = _content_rows(engine, schema, cp)
        assert [r["sort_order"] for r in content] == [1, 2, 3]
        assert all(r["cp"] == cp for r in content)
        assert content[0]["heading_text"] == "Heading 1"
        assert content[0]["content_text"] == "body 1"
        assert content[0]["page_start"] == 1
        assert content[0]["page_end"] == 1

    def test_rerun_skips_existing_without_overwrite(
        self, ephemeral_schema: tuple["Engine", str], tmp_path: Path
    ) -> None:
        """Re-running a load with overwrite=False skips: table state unchanged.

        The skipped document is still a survivor — an already-ingested document
        counts once in ``ok_files`` and is not a failure (real-DB idempotency).
        """
        engine, schema = ephemeral_schema
        _ensure_document_tables(engine, schema)
        cleaned_dir = tmp_path / "cleaned"
        _write_cleaned(cleaned_dir, "ge101c01", _cleaned_doc(n=2))
        cp = "cms_iom.pub_100_01.ge101c01"
        load_kwargs = dict(
            cleaned_dir=str(cleaned_dir),
            file_paths=["ge101c01.pdf"],
            engine=engine,
            db_schema=schema,
            overwrite=False,
            collection_paths={"ge101c01.pdf": cp},
            document_titles={"ge101c01.pdf": "Chapter 1"},
        )
        step_load(**load_kwargs)
        # Change the on-disk payload so a (wrong) re-load would be visible.
        _write_cleaned(cleaned_dir, "ge101c01", _cleaned_doc(n=3, binary_hash=999))

        ok_files, failures = step_load(**load_kwargs)

        # Counted exactly once as a survivor; the stored rows are the FIRST
        # load's (2 sections, original hash) — nothing was replaced.
        assert ok_files == ["ge101c01.pdf"]
        assert failures == []
        assert _table_counts(engine, schema) == (1, 2)
        doc = _doc_row(engine, schema, cp)
        assert doc["n_parsed_sections"] == 2
        assert int(doc["source_binary_hash"]) == FIXTURE_BINARY_HASH

    def test_rerun_overwrite_replaces_cleanly(
        self, ephemeral_schema: tuple["Engine", str], tmp_path: Path
    ) -> None:
        """overwrite=True deletes the existing document and re-ingests it.

        After the second run the table holds exactly the NEW payload — new
        section count, new hash, no leftover content rows from the first load
        (the delete cascades).
        """
        engine, schema = ephemeral_schema
        _ensure_document_tables(engine, schema)
        cleaned_dir = tmp_path / "cleaned"
        _write_cleaned(cleaned_dir, "ge101c01", _cleaned_doc(n=3))
        cp = "cms_iom.pub_100_01.ge101c01"
        load_kwargs = dict(
            cleaned_dir=str(cleaned_dir),
            file_paths=["ge101c01.pdf"],
            engine=engine,
            db_schema=schema,
            overwrite=True,
            collection_paths={"ge101c01.pdf": cp},
            document_titles={"ge101c01.pdf": "Chapter 1"},
        )
        step_load(**load_kwargs)
        _write_cleaned(cleaned_dir, "ge101c01", _cleaned_doc(n=2, binary_hash=777))

        ok_files, failures = step_load(**load_kwargs)

        assert ok_files == ["ge101c01.pdf"]
        assert failures == []
        # Exactly the second payload: 1 document, 2 content rows, new hash.
        assert _table_counts(engine, schema) == (1, 2)
        doc = _doc_row(engine, schema, cp)
        assert doc["n_parsed_sections"] == 2
        assert int(doc["source_binary_hash"]) == 777
        assert [r["sort_order"] for r in _content_rows(engine, schema, cp)] == [1, 2]

    def test_failed_load_commits_no_partial_rows_and_batch_continues(
        self, ephemeral_schema: tuple["Engine", str], tmp_path: Path
    ) -> None:
        """A load failing mid-document rolls back whole; the next file loads.

        The atomicity test the old mock could never provide: a constraint added
        to the content table makes the FIRST document's second section fail
        mid-transaction, proving the document row and its first section roll
        back together (no partial rows committed) while the second document —
        its own transaction — still loads.
        """
        engine, schema = ephemeral_schema
        _ensure_document_tables(engine, schema)
        # Sabotage: section 2 of any document violates this check, so a
        # multi-section insert fails AFTER the document row and section 1
        # were already executed in the same transaction.
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"alter table {schema}.document_content "
                    "add constraint fail_second check (sort_order <> 2)"
                )
            )
        cleaned_dir = tmp_path / "cleaned"
        _write_cleaned(cleaned_dir, "first", _cleaned_doc(n=3))
        second_payload = _cleaned_doc(n=1)
        _write_cleaned(cleaned_dir, "second", second_payload)

        ok_files, failures = step_load(
            cleaned_dir=str(cleaned_dir),
            file_paths=["first.pdf", "second.pdf"],
            engine=engine,
            db_schema=schema,
            overwrite=False,
            collection_paths={
                "first.pdf": "cms_iom.pub.first",
                "second.pdf": "cms_iom.pub.second",
            },
            document_titles={"first.pdf": "First", "second.pdf": "Second"},
        )

        # The first file failed and was recorded; the second still loaded.
        assert ok_files == ["second.pdf"]
        assert len(failures) == 1
        assert failures[0]["file"] == "first.pdf"
        assert failures[0]["stage"] == "load"
        # NOTHING of the first document was committed — not the document row,
        # not section 1 (which had already executed before section 2 failed).
        assert _doc_row(engine, schema, "cms_iom.pub.first") is None
        assert _content_rows(engine, schema, "cms_iom.pub.first") == []
        # The second document is fully present (1 doc row + 1 content row).
        assert _doc_row(engine, schema, "cms_iom.pub.second") is not None
        assert _table_counts(engine, schema) == (1, 1)

    def test_blank_title_and_blank_section_rejected_by_postgres(
        self, ephemeral_schema: tuple["Engine", str]
    ) -> None:
        """The new CHECK constraints hold at rest, not merely in Pydantic.

        A whitespace-only title and a section whose heading and content are
        both whitespace-only are rejected by PostgreSQL itself (defense in
        depth for any path that bypasses the load-step validation).
        """
        from sqlalchemy.exc import IntegrityError

        engine, schema = ephemeral_schema
        _ensure_document_tables(engine, schema)

        # Whitespace-only title violates check (trim(title) <> '').
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        f"insert into {schema}.document "
                        "(collection_path, title, n_parsed_sections, source_binary_hash) "
                        "values ('cms_iom.pub.blank', '   ', 1, 1)"
                    )
                )

        # A both-blank section violates the strengthened not-both-empty guard.
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"insert into {schema}.document "
                    "(collection_path, title, n_parsed_sections, source_binary_hash) "
                    "values ('cms_iom.pub.ok', 'Real title', 1, 1)"
                )
            )
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        f"insert into {schema}.document_content "
                        "(collection_path, sort_order, heading_text, content_text, word_count) "
                        "values ('cms_iom.pub.ok', 1, '  ', '', 0)"
                    )
                )
        # A real heading with an empty body still passes (heading-only rows
        # are legitimate sections).
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"insert into {schema}.document_content "
                    "(collection_path, sort_order, heading_text, content_text, word_count) "
                    "values ('cms_iom.pub.ok', 1, 'Heading', null, 1)"
                )
            )

    def test_multi_file_batch_skips_existing_then_inserts_next(
        self, ephemeral_schema: tuple["Engine", str], tmp_path: Path
    ) -> None:
        """Two files in one batch: the first (existing) skips, the second inserts.

        Pins that per-document transactions are independent and an early skip
        does not abort the loop. Both files are survivors, counted once each.
        """
        engine, schema = ephemeral_schema
        _ensure_document_tables(engine, schema)
        cleaned_dir = tmp_path / "cleaned"
        _write_cleaned(cleaned_dir, "first", _cleaned_doc(n=2))
        _write_cleaned(cleaned_dir, "second", _cleaned_doc(n=2))
        paths = {
            "first.pdf": "cms_iom.pub.first",
            "second.pdf": "cms_iom.pub.second",
        }
        titles = {"first.pdf": "First", "second.pdf": "Second"}
        # Pre-load ONLY the first file so it is "existing" for the batch run.
        step_load(
            cleaned_dir=str(cleaned_dir),
            file_paths=["first.pdf"],
            engine=engine,
            db_schema=schema,
            overwrite=False,
            collection_paths=paths,
            document_titles=titles,
        )

        ok_files, failures = step_load(
            cleaned_dir=str(cleaned_dir),
            file_paths=["first.pdf", "second.pdf"],
            engine=engine,
            db_schema=schema,
            overwrite=False,
            collection_paths=paths,
            document_titles=titles,
        )

        # Both files are survivors — the skip branch records its file once.
        assert ok_files == ["first.pdf", "second.pdf"]
        assert failures == []
        # 2 documents, 2 content rows each; the first was not duplicated.
        assert _table_counts(engine, schema) == (2, 4)

    def test_malformed_cleaned_json_records_failure(
        self, tmp_path: Path
    ) -> None:
        """A cleaned JSON that violates the schema is recorded, not raised."""
        cleaned_dir = tmp_path / "cleaned"
        # n_parsed_sections disagrees with len(sections): CleanedDocument rejects it.
        bad = {"document": {"n_parsed_sections": 5, "binary_hash": 1}, "sections": []}
        _write_cleaned(cleaned_dir, "ge101c01", bad)
        engine = MagicMock()

        ok_files, failures = step_load(
            cleaned_dir=str(cleaned_dir),
            file_paths=["ge101c01.pdf"],
            engine=engine,
            db_schema="cms_iom",
            overwrite=False,
            collection_paths={"ge101c01.pdf": "cms_iom.pub_100_01.ge101c01"},
            document_titles={"ge101c01.pdf": "Chapter 1"},
        )

        # model_validate_json runs before engine.begin(), so the malformed file
        # is rejected before ANY database touch — no transaction is opened.
        engine.begin.assert_not_called()
        engine.connect.assert_not_called()
        assert ok_files == []
        assert len(failures) == 1
        assert failures[0]["file"] == "ge101c01.pdf"
        assert failures[0]["stage"] == "load"

    def test_missing_title_propagates_keyerror(self, tmp_path: Path) -> None:
        """Title is required: a file absent from document_titles propagates KeyError.

        ``main()`` guarantees a document_titles entry for every file, so a missing
        entry is a programming bug, not a data error. It is NOT caught per-file;
        the KeyError propagates (to be surfaced by main()'s pipeline handler).
        """
        cleaned_dir = tmp_path / "cleaned"
        _write_cleaned(cleaned_dir, "ge101c01", _cleaned_doc())
        engine = MagicMock()

        with pytest.raises(KeyError):
            step_load(
                cleaned_dir=str(cleaned_dir),
                file_paths=["ge101c01.pdf"],
                engine=engine,
                db_schema="cms_iom",
                overwrite=False,
                collection_paths={"ge101c01.pdf": "cms_iom.pub_100_01.ge101c01"},
                document_titles={},
            )

    def test_missing_cleaned_json_records_failure(self, tmp_path: Path) -> None:
        """A missing cleaned JSON is recorded as a failure before touching the DB."""
        cleaned_dir = tmp_path / "cleaned"
        cleaned_dir.mkdir()
        engine = MagicMock()

        ok_files, failures = step_load(
            cleaned_dir=str(cleaned_dir),
            file_paths=["ge101c01.pdf"],
            engine=engine,
            db_schema="cms_iom",
            overwrite=False,
            collection_paths={"ge101c01.pdf": "cms_iom.pub_100_01.ge101c01"},
            document_titles={"ge101c01.pdf": "Chapter 1"},
        )

        engine.begin.assert_not_called()
        assert ok_files == []
        assert len(failures) == 1
        assert failures[0]["file"] == "ge101c01.pdf"
        assert failures[0]["stage"] == "load"
        assert "ge101c01.json" in failures[0]["reason"]

    def test_missing_collection_path_entry_propagates_keyerror(
        self, tmp_path: Path
    ) -> None:
        """A file absent from collection_paths propagates KeyError (programming bug).

        ``collection_paths`` is built by ``main()`` and holds an entry for every
        file; a missing key is a programming bug, not a data error, so it is NOT
        caught per-file — the KeyError propagates to main()'s pipeline handler.
        """
        cleaned_dir = tmp_path / "cleaned"
        _write_cleaned(cleaned_dir, "ge101c01", _cleaned_doc())
        engine = MagicMock()

        with pytest.raises(KeyError):
            step_load(
                cleaned_dir=str(cleaned_dir),
                file_paths=["ge101c01.pdf"],
                engine=engine,
                db_schema="cms_iom",
                overwrite=False,
                collection_paths={},  # no entry for ge101c01.pdf
                document_titles={"ge101c01.pdf": "Chapter 1"},
            )

    def test_mixed_batch_records_bad_loads_good(
        self, ephemeral_schema: tuple["Engine", str], tmp_path: Path
    ) -> None:
        """One file failing to load does not abort the batch; the good one loads.

        The first file's cleaned JSON is missing (recorded as a failure); the
        second has a valid cleaned JSON and is inserted for real.
        """
        engine, schema = ephemeral_schema
        _ensure_document_tables(engine, schema)
        cleaned_dir = tmp_path / "cleaned"
        cleaned_dir.mkdir()
        # bad.json deliberately absent; good.json valid.
        _write_cleaned(cleaned_dir, "good", _cleaned_doc(n=2))

        ok_files, failures = step_load(
            cleaned_dir=str(cleaned_dir),
            file_paths=["bad.pdf", "good.pdf"],
            engine=engine,
            db_schema=schema,
            overwrite=False,
            collection_paths={
                "bad.pdf": "cms_iom.pub.bad",
                "good.pdf": "cms_iom.pub.good",
            },
            document_titles={"bad.pdf": "Bad", "good.pdf": "Good"},
        )

        # The good file loaded despite the bad file failing first.
        assert ok_files == ["good.pdf"]
        assert len(failures) == 1
        assert failures[0]["file"] == "bad.pdf"
        assert failures[0]["stage"] == "load"
        # Only the good document reached the database (1 doc + 2 content rows).
        assert _doc_row(engine, schema, "cms_iom.pub.bad") is None
        assert _doc_row(engine, schema, "cms_iom.pub.good") is not None
        assert _table_counts(engine, schema) == (1, 2)

    def test_undecodable_cleaned_json_records_failure(self, tmp_path: Path) -> None:
        """A cleaned JSON that is not valid UTF-8 is recorded, not raised.

        The read happens before any transaction opens, and a UnicodeDecodeError
        (a ValueError) is a per-file data failure like any other unreadable
        cleaned file — it must not abort the remaining documents.
        """
        cleaned_dir = tmp_path / "cleaned"
        cleaned_dir.mkdir()
        # Invalid UTF-8 start byte: read_text(encoding="utf-8") raises.
        (cleaned_dir / "ge101c01.json").write_bytes(b"\xff\xfe not utf-8")
        engine = MagicMock()

        ok_files, failures = step_load(
            cleaned_dir=str(cleaned_dir),
            file_paths=["ge101c01.pdf"],
            engine=engine,
            db_schema="cms_iom",
            overwrite=False,
            collection_paths={"ge101c01.pdf": "cms_iom.pub_100_01.ge101c01"},
            document_titles={"ge101c01.pdf": "Chapter 1"},
        )

        engine.begin.assert_not_called()
        assert ok_files == []
        assert len(failures) == 1
        assert failures[0]["file"] == "ge101c01.pdf"
        assert failures[0]["stage"] == "load"


def _empty_env(tmp_path: Path) -> str:
    """Write an empty dotenv file for the required ``--env-file`` flag.

    These tests exercise config handling and pipeline wiring, not credentials:
    an empty file satisfies the flag while loading nothing, so the ambient
    test environment is untouched.

    Args:
        tmp_path: pytest tmp_path fixture directory.

    Returns:
        Absolute path string to the written (empty) dotenv file.
    """
    env_path = tmp_path / ".env.empty"
    env_path.write_text("", encoding="utf-8")
    return str(env_path)


def _mark_instance(tmp_path: Path) -> None:
    """Mark ``tmp_path`` as an instance root so relative config paths resolve.

    main() anchors a config's relative paths (source_dir/parsed_dir/
    cleaned_dir) to the nearest ancestor of ``--config`` that contains a
    pyproject.toml; without the marker the run aborts with
    InstanceRootNotFoundError.

    Args:
        tmp_path: pytest tmp_path fixture directory holding the test config.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "test-instance"\n', encoding="utf-8"
    )


class TestMainAllInvalidConfig:
    """Tests for main()'s fail-fast on a wholly-invalid config (review fix 4.1)."""

    def test_all_documents_invalid_exits_nonzero(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A config whose every collection_path is invalid exits 1, not 0.

        Without the 4.1 guard the steps would iterate over an empty document
        list and main() would log SUCCESS and exit 0 (fail-silent). The guard
        turns a wholly-invalid config into a visible non-zero exit.
        """
        config_path = tmp_path / "all_invalid.toml"
        config_path.write_text(
            "\n".join(
                [
                    "[module]",
                    'name = "test_mod"',
                    'source_dir = "src"',
                    "",
                    "[[module.documents]]",
                    'file = "a.pdf"',
                    'title = "A"',
                    'collection_path = "cms_iom.Bad-A"',  # uppercase/hyphen: invalid ltree
                    "",
                    "[load]",
                    "run = false",  # never reach the DB; the guard fires first
                ]
            ),
            encoding="utf-8",
        )
        mocker.patch(
            "sys.argv",
            ["ingest.py", "--config", str(config_path), "--env-file", _empty_env(tmp_path)],
        )
        # Avoid creating real log files during the test.
        mocker.patch("ingpipe_file_ingestion.ingest.setup_entry_logging")

        with pytest.raises(SystemExit) as exc:
            main()

        assert exc.value.code == 1


def _valid_config(
    *, run_parse: bool = True, run_clean: bool = True, run_load: bool = True
) -> str:
    """Build a TOML config string with one valid document and per-step run flags.

    Args:
        run_parse: Value for ``[parse].run``.
        run_clean: Value for ``[clean].run``.
        run_load: Value for ``[load].run``.

    Returns:
        A TOML document string suitable for writing under ``tmp_path``.
    """
    return "\n".join(
        [
            "[module]",
            'name = "test_mod"',
            'source_dir = "src"',
            "",
            "[[module.documents]]",
            'file = "a.pdf"',
            'title = "A"',
            'collection_path = "cms_iom.pub.a"',
            "",
            "[parse]",
            f"run = {str(run_parse).lower()}",
            'parsed_dir = "parsed"',
            "",
            "[clean]",
            f"run = {str(run_clean).lower()}",
            'cleaned_dir = "cleaned"',
            "",
            "[load]",
            f"run = {str(run_load).lower()}",
            'db_name = "ragdb"',
            'db_schema = "cms_iom"',
        ]
    )


class TestMain:
    """Tests for main() argument/config handling and pipeline error branches.

    Each test patches ``sys.argv`` and ``ingest.setup_logging`` (which runs
    before the config-exists check and would otherwise create real log dirs),
    writes a TOML config under ``tmp_path``, and mocks the pipeline steps so no
    real parsing/DB access occurs.
    """

    def test_config_file_not_found_exits_nonzero(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A --config path that does not exist exits 1."""
        missing = tmp_path / "nope.toml"
        mocker.patch(
            "sys.argv",
            ["ingest.py", "--config", str(missing), "--env-file", _empty_env(tmp_path)],
        )
        mocker.patch("ingpipe_file_ingestion.ingest.setup_entry_logging")

        with pytest.raises(SystemExit) as exc:
            main()

        assert exc.value.code == 1

    def test_malformed_toml_exits_nonzero(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A config that fails to parse as TOML exits 1 (TOMLDecodeError branch)."""
        config_path = tmp_path / "bad.toml"
        config_path.write_text("this is = not valid = toml ][", encoding="utf-8")
        mocker.patch(
            "sys.argv",
            ["ingest.py", "--config", str(config_path), "--env-file", _empty_env(tmp_path)],
        )
        mocker.patch("ingpipe_file_ingestion.ingest.setup_entry_logging")

        with pytest.raises(SystemExit) as exc:
            main()

        assert exc.value.code == 1

    def test_missing_required_config_field_exits_nonzero(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A config missing a required field ([module].source_dir) exits 1.

        The omitted key surfaces as a KeyError in the config-extraction block,
        which main() maps to exit 1.
        """
        config_path = tmp_path / "missing_field.toml"
        config_path.write_text(
            "\n".join(
                [
                    "[module]",
                    'name = "test_mod"',
                    # source_dir deliberately omitted
                    "",
                    "[[module.documents]]",
                    'file = "a.pdf"',
                    'title = "A"',
                    'collection_path = "cms_iom.pub.a"',
                ]
            ),
            encoding="utf-8",
        )
        mocker.patch(
            "sys.argv",
            ["ingest.py", "--config", str(config_path), "--env-file", _empty_env(tmp_path)],
        )
        mocker.patch("ingpipe_file_ingestion.ingest.setup_entry_logging")

        with pytest.raises(SystemExit) as exc:
            main()

        assert exc.value.code == 1

    def test_invalid_max_pages_per_batch_exits_nonzero(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A negative [parse].max_pages_per_batch is rejected as a config error."""
        config_path = tmp_path / "bad_batch.toml"
        config_path.write_text(
            "\n".join(
                [
                    "[module]",
                    'source_dir = "src"',
                    "",
                    "[[module.documents]]",
                    'file = "a.pdf"',
                    'title = "A"',
                    'collection_path = "cms_iom.pub.a"',
                    "",
                    "[parse]",
                    "max_pages_per_batch = -5",
                ]
            ),
            encoding="utf-8",
        )
        mocker.patch(
            "sys.argv",
            ["ingest.py", "--config", str(config_path), "--env-file", _empty_env(tmp_path)],
        )
        mocker.patch("ingpipe_file_ingestion.ingest.setup_entry_logging")

        with pytest.raises(SystemExit) as exc:
            main()

        assert exc.value.code == 1

    def test_non_boolean_overwrite_exits_before_any_step(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A quoted `overwrite = "false"` is a config error, not a truthy string.

        Without the guard the truthy string would enable the destructive
        overwrite path (DELETE of the document row cascading to its content).
        The guard fires during config extraction, before any step or DB access.
        """
        config_path = tmp_path / "config.toml"
        # The key must precede the first table header to land at the top level.
        config_path.write_text(
            'overwrite = "false"\n' + _valid_config(), encoding="utf-8"
        )
        _mark_instance(tmp_path)
        mocker.patch(
            "sys.argv",
            ["ingest.py", "--config", str(config_path), "--env-file", _empty_env(tmp_path)],
        )
        mocker.patch("ingpipe_file_ingestion.ingest.setup_entry_logging")
        mock_parse = mocker.patch("ingpipe_file_ingestion.ingest.step_parse")
        mock_load = mocker.patch("ingpipe_file_ingestion.ingest.step_load")
        mock_engine = mocker.patch("ingpipe_file_ingestion.ingest.get_engine")

        with pytest.raises(SystemExit) as exc:
            main()

        assert exc.value.code == 1
        mock_parse.assert_not_called()
        mock_load.assert_not_called()
        mock_engine.assert_not_called()

    def test_boolean_overwrite_and_cli_flag_override(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A real boolean passes the guard, and --overwrite overrides the TOML.

        The config says overwrite = false; the CLI flag flips it to True for
        every step (CLI-over-TOML precedence, matching generate_embeddings).
        """
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            "overwrite = false\n" + _valid_config(), encoding="utf-8"
        )
        _mark_instance(tmp_path)
        mocker.patch(
            "sys.argv",
            [
                "ingest.py",
                "--config", str(config_path),
                "--overwrite",
                "--env-file", _empty_env(tmp_path),
            ],
        )
        mocker.patch("ingpipe_file_ingestion.ingest.setup_entry_logging")
        mock_parse = mocker.patch(
            "ingpipe_file_ingestion.ingest.step_parse", return_value=(["a.pdf"], [])
        )
        mock_clean = mocker.patch(
            "ingpipe_file_ingestion.ingest.step_clean", return_value=(["a.pdf"], [])
        )
        mock_load = mocker.patch(
            "ingpipe_file_ingestion.ingest.step_load", return_value=(["a.pdf"], [])
        )
        mocker.patch("ingpipe_file_ingestion.ingest.get_engine", return_value=MagicMock())
        mocker.patch("ingpipe_file_ingestion.ingest.require_extensions")
        mocker.patch("ingpipe_file_ingestion.ingest.ensure_schema")

        main()

        # step_parse(source_dir, file_paths, parsed_dir, overwrite, ...)
        assert mock_parse.call_args.args[3] is True
        # step_clean(parsed_dir, cleaned_dir, file_paths, overwrite)
        assert mock_clean.call_args.args[3] is True
        # step_load(cleaned_dir, file_paths, engine, db_schema, overwrite, ...)
        assert mock_load.call_args.args[4] is True

    def test_missing_parsed_dir_with_parse_enabled_exits_nonzero(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """An enabled parse step with no [parse].parsed_dir is a config error.

        Without the guard the missing key defaults to ``""``, ``Path("")``
        resolves to ``.``, and the run would silently write its parsed JSON into
        the process working directory. The step must not be reached at all.
        """
        config_path = tmp_path / "no_parsed_dir.toml"
        config_path.write_text(
            "\n".join(
                [
                    "[module]",
                    'source_dir = "src"',
                    "",
                    "[[module.documents]]",
                    'file = "a.pdf"',
                    'title = "A"',
                    'collection_path = "cms_iom.pub.a"',
                    "",
                    "[parse]",
                    "run = true",  # parsed_dir deliberately omitted
                    "",
                    "[clean]",
                    "run = false",
                    "",
                    "[load]",
                    "run = false",
                ]
            ),
            encoding="utf-8",
        )
        mocker.patch(
            "sys.argv",
            ["ingest.py", "--config", str(config_path), "--env-file", _empty_env(tmp_path)],
        )
        mocker.patch("ingpipe_file_ingestion.ingest.setup_entry_logging")
        mock_parse = mocker.patch("ingpipe_file_ingestion.ingest.step_parse")

        with pytest.raises(SystemExit) as exc:
            main()

        assert exc.value.code == 1
        mock_parse.assert_not_called()

    def test_missing_cleaned_dir_with_clean_enabled_exits_nonzero(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """An enabled clean step with no [clean].cleaned_dir is a config error."""
        config_path = tmp_path / "no_cleaned_dir.toml"
        config_path.write_text(
            "\n".join(
                [
                    "[module]",
                    'source_dir = "src"',
                    "",
                    "[[module.documents]]",
                    'file = "a.pdf"',
                    'title = "A"',
                    'collection_path = "cms_iom.pub.a"',
                    "",
                    "[parse]",
                    "run = false",
                    'parsed_dir = "parsed"',
                    "",
                    "[clean]",
                    "run = true",  # cleaned_dir deliberately omitted
                    "",
                    "[load]",
                    "run = false",
                ]
            ),
            encoding="utf-8",
        )
        mocker.patch(
            "sys.argv",
            ["ingest.py", "--config", str(config_path), "--env-file", _empty_env(tmp_path)],
        )
        mocker.patch("ingpipe_file_ingestion.ingest.setup_entry_logging")
        mock_clean = mocker.patch("ingpipe_file_ingestion.ingest.step_clean")

        with pytest.raises(SystemExit) as exc:
            main()

        assert exc.value.code == 1
        mock_clean.assert_not_called()

    def test_pipeline_known_error_exits_nonzero(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A step raising a known error (ValueError) is caught and exits 1.

        Covers the ``except (FileNotFoundError, OSError, ValueError,
        RuntimeError)`` pipeline handler.
        """
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            _valid_config(run_clean=False, run_load=False), encoding="utf-8"
        )
        _mark_instance(tmp_path)
        mocker.patch(
            "sys.argv",
            ["ingest.py", "--config", str(config_path), "--env-file", _empty_env(tmp_path)],
        )
        mocker.patch("ingpipe_file_ingestion.ingest.setup_entry_logging")
        mocker.patch(
            "ingpipe_file_ingestion.ingest.step_parse", side_effect=ValueError("bad parse")
        )

        with pytest.raises(SystemExit) as exc:
            main()

        assert exc.value.code == 1

    def test_pipeline_unexpected_error_exits_nonzero(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A step raising an unexpected error (TypeError) hits the broad-except, exits 1.

        TypeError is not in the named handler tuple, so it falls through to the
        broad ``except Exception`` path.
        """
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            _valid_config(run_clean=False, run_load=False), encoding="utf-8"
        )
        _mark_instance(tmp_path)
        mocker.patch(
            "sys.argv",
            ["ingest.py", "--config", str(config_path), "--env-file", _empty_env(tmp_path)],
        )
        mocker.patch("ingpipe_file_ingestion.ingest.setup_entry_logging")
        mocker.patch(
            "ingpipe_file_ingestion.ingest.step_parse", side_effect=TypeError("unexpected")
        )

        with pytest.raises(SystemExit) as exc:
            main()

        assert exc.value.code == 1

    def test_all_steps_disabled_skips_and_succeeds(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """With every run flag false, no step runs and main() completes (exit 0).

        Pins the run-flag gating: the disabled-step branches are taken and no
        step_* mock is invoked.
        """
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            _valid_config(run_parse=False, run_clean=False, run_load=False),
            encoding="utf-8",
        )
        _mark_instance(tmp_path)
        mocker.patch(
            "sys.argv",
            ["ingest.py", "--config", str(config_path), "--env-file", _empty_env(tmp_path)],
        )
        mocker.patch("ingpipe_file_ingestion.ingest.setup_entry_logging")
        mock_parse = mocker.patch("ingpipe_file_ingestion.ingest.step_parse")
        mock_clean = mocker.patch("ingpipe_file_ingestion.ingest.step_clean")
        mock_load = mocker.patch("ingpipe_file_ingestion.ingest.step_load")
        mock_engine = mocker.patch("ingpipe_file_ingestion.ingest.get_engine")

        # No SystemExit: a fully-skipped run is a successful no-op.
        main()

        mock_parse.assert_not_called()
        mock_clean.assert_not_called()
        mock_load.assert_not_called()
        mock_engine.assert_not_called()

    def test_all_steps_enabled_happy_path_runs_each_step(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """With every run flag true, each step (incl. the load block) runs once.

        Covers the enabled-step branches and the load block's get_engine /
        ensure_schema / step_load wiring, all mocked so no real DB is touched.
        """
        config_path = tmp_path / "config.toml"
        config_path.write_text(_valid_config(), encoding="utf-8")
        _mark_instance(tmp_path)
        mocker.patch(
            "sys.argv",
            ["ingest.py", "--config", str(config_path), "--env-file", _empty_env(tmp_path)],
        )
        mocker.patch("ingpipe_file_ingestion.ingest.setup_entry_logging")
        # Each step now returns (ok_files, failures); the single config document
        # survives every stage with no failures.
        mock_parse = mocker.patch(
            "ingpipe_file_ingestion.ingest.step_parse", return_value=(["a.pdf"], [])
        )
        mock_clean = mocker.patch(
            "ingpipe_file_ingestion.ingest.step_clean", return_value=(["a.pdf"], [])
        )
        mock_load = mocker.patch(
            "ingpipe_file_ingestion.ingest.step_load", return_value=(["a.pdf"], [])
        )
        mock_engine = mocker.patch(
            "ingpipe_file_ingestion.ingest.get_engine", return_value=MagicMock()
        )
        mocker.patch("ingpipe_file_ingestion.ingest.require_extensions")
        mock_ensure = mocker.patch("ingpipe_file_ingestion.ingest.ensure_schema")

        # A fully-enabled, fully-mocked run completes without SystemExit.
        main()

        mock_parse.assert_called_once()
        mock_clean.assert_called_once()
        mock_engine.assert_called_once()
        mock_ensure.assert_called_once()
        mock_load.assert_called_once()
        # Relative config dirs are anchored to the instance root (tmp_path,
        # marked by _mark_instance), not the working directory.
        parse_args = mock_parse.call_args.args
        assert parse_args[0] == str(tmp_path / "src")
        assert parse_args[2] == str(tmp_path / "parsed")
        # Without comment keys in [load], ensure_schema receives None for each
        # override (leave existing comments alone).
        ensure_kwargs = mock_ensure.call_args.kwargs
        assert ensure_kwargs["schema_comment"] is None
        assert ensure_kwargs["document_table_comment"] is None
        assert ensure_kwargs["content_table_comment"] is None

    def test_load_comment_keys_pass_through_to_ensure_schema(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """The optional [load] comment keys are parsed and reach ensure_schema."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            _valid_config(run_parse=False, run_clean=False)
            + "\n"
            + "\n".join(
                [
                    'schema_comment = "Schema — flavored text"',
                    'document_table_comment = "Doc table text"',
                    'content_table_comment = "Content table text"',
                ]
            ),
            encoding="utf-8",
        )
        _mark_instance(tmp_path)
        mocker.patch(
            "sys.argv",
            ["ingest.py", "--config", str(config_path), "--env-file", _empty_env(tmp_path)],
        )
        mocker.patch("ingpipe_file_ingestion.ingest.setup_entry_logging")
        mocker.patch("ingpipe_file_ingestion.ingest.step_load", return_value=(["a.pdf"], []))
        mocker.patch("ingpipe_file_ingestion.ingest.get_engine", return_value=MagicMock())
        mocker.patch("ingpipe_file_ingestion.ingest.require_extensions")
        mock_ensure = mocker.patch("ingpipe_file_ingestion.ingest.ensure_schema")

        main()

        ensure_kwargs = mock_ensure.call_args.kwargs
        assert ensure_kwargs["schema_comment"] == "Schema — flavored text"
        assert ensure_kwargs["document_table_comment"] == "Doc table text"
        assert ensure_kwargs["content_table_comment"] == "Content table text"

    def test_non_string_load_comment_key_exits_nonzero(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """A non-string [load] comment value is a config error (exit 1), not a TypeError."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            _valid_config(run_parse=False, run_clean=False)
            + "\nschema_comment = 42\n",
            encoding="utf-8",
        )
        mocker.patch(
            "sys.argv",
            ["ingest.py", "--config", str(config_path), "--env-file", _empty_env(tmp_path)],
        )
        mocker.patch("ingpipe_file_ingestion.ingest.setup_entry_logging")
        mock_ensure = mocker.patch("ingpipe_file_ingestion.ingest.ensure_schema")

        with pytest.raises(SystemExit) as exc:
            main()

        assert exc.value.code == 1
        mock_ensure.assert_not_called()

    def test_some_files_fail_exits_nonzero_with_summary(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """When a step reports a failure, main() logs a summary and exits 1.

        The clean step drops the only document (records a failure); main()
        aggregates that failure, logs the pipeline summary, and exits non-zero
        so the loss is surfaced.
        """
        config_path = tmp_path / "config.toml"
        config_path.write_text(_valid_config(run_load=False), encoding="utf-8")
        _mark_instance(tmp_path)
        mocker.patch(
            "sys.argv",
            ["ingest.py", "--config", str(config_path), "--env-file", _empty_env(tmp_path)],
        )
        mocker.patch("ingpipe_file_ingestion.ingest.setup_entry_logging")
        mocker.patch("ingpipe_file_ingestion.ingest.step_parse", return_value=(["a.pdf"], []))
        mocker.patch(
            "ingpipe_file_ingestion.ingest.step_clean",
            return_value=(
                [],
                [{"file": "a.pdf", "stage": "clean", "reason": "boom"}],
            ),
        )

        import logging as logging_module
        with self._caplog.at_level(logging_module.INFO):
            with pytest.raises(SystemExit) as exc:
                main()

        assert exc.value.code == 1
        # The pipeline summary logs from ingest; the failure tail logs from
        # the shared preamble (ingpipe_lib.cli) — both must surface.
        messages = " ".join(r.message for r in self._caplog.records)
        assert "Pipeline summary" in messages
        assert "boom" in messages
        assert "clean: a.pdf" in messages

    @pytest.fixture(autouse=True)
    def _attach_caplog(self, caplog: pytest.LogCaptureFixture) -> None:
        """Expose caplog to tests that assert on multi-module log output."""
        self._caplog = caplog

    def test_all_files_succeed_exits_zero_with_summary(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """When every file survives, main() logs SUCCESS and does not exit non-zero."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            _valid_config(run_clean=False, run_load=False), encoding="utf-8"
        )
        _mark_instance(tmp_path)
        mocker.patch(
            "sys.argv",
            ["ingest.py", "--config", str(config_path), "--env-file", _empty_env(tmp_path)],
        )
        mocker.patch("ingpipe_file_ingestion.ingest.setup_entry_logging")
        mocker.patch("ingpipe_file_ingestion.ingest.step_parse", return_value=(["a.pdf"], []))

        import logging as logging_module
        # No SystemExit: an all-success run is a clean exit 0.
        with self._caplog.at_level(logging_module.INFO):
            main()

        messages = " ".join(r.message for r in self._caplog.records)
        assert "Pipeline summary" in messages
        assert "SUCCESS" in messages


class TestGetEngine:
    """Tests for get_engine environment-variable handling."""

    def test_missing_env_var_raises_value_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing required POSTGRES_* env var raises a clear ValueError.

        The var is removed explicitly (raising=False) because the shell or an
        earlier ``load_env`` call may have populated it; importing ``ingest``
        no longer does, but the removal keeps this test independent of both.
        """
        monkeypatch.delenv("POSTGRES_HOST", raising=False)

        with pytest.raises(ValueError, match="Missing Postgres environment variable"):
            get_engine("ragdb")

    def test_non_integer_port_raises_value_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-numeric POSTGRES_PORT is rejected with the offending value."""
        monkeypatch.setenv("POSTGRES_HOST", "db.example.org")
        monkeypatch.setenv("POSTGRES_PORT", "not-a-number")
        monkeypatch.setenv("POSTGRES_USER", "rag_user")
        monkeypatch.setenv("POSTGRES_PASSWORD", "pw")

        with pytest.raises(ValueError, match="POSTGRES_PORT is not an integer"):
            get_engine("ragdb")

    def test_reserved_characters_in_password_survive_url_build(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Credentials with URL-reserved characters round-trip through URL.create.

        ``URL.create`` percent-encodes the rendered URL while keeping the parsed
        components intact, which is the whole point of building the URL from
        parts: a hand-concatenated connection string would corrupt a password
        containing ``@``, ``:`` or ``/``. ``create_engine`` is left real — it
        only resolves the dialect, it does not connect.
        """
        monkeypatch.setenv("POSTGRES_HOST", "db.example.org")
        monkeypatch.setenv("POSTGRES_PORT", "5432")
        monkeypatch.setenv("POSTGRES_USER", "rag_user")
        monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss:w/rd")

        engine = get_engine("ragdb")

        assert engine.url.password == "p@ss:w/rd"
        assert engine.url.username == "rag_user"
        assert engine.url.host == "db.example.org"
        assert engine.url.port == 5432
        assert engine.url.database == "ragdb"


class TestEnvFile:
    """Tests for the removal of the import-time environment load."""

    def test_import_does_not_mutate_postgres_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Importing the module leaves the four POSTGRES_* variables alone.

        The module used to call ``load_dotenv()`` at module scope, so merely
        importing it populated the process environment from whatever ``.env``
        the working directory happened to sit above. Credentials are now
        resolved inside ``main()`` from ``--env-file``, so a fresh import must
        be inert.
        """
        # Arrange: clear the four variables, then re-execute the module body.
        for var in POSTGRES_VARS:
            monkeypatch.delenv(var, raising=False)
        # The reload re-runs ``from ingpipe_file_ingestion._utils import ...``,
        # and three directories
        # in this repo hold a module of that bare name. In a whole-repo run a
        # sibling suite's copy is already cached and would be served instead,
        # so pin this module's directory ahead of theirs and evict the foreign
        # copy. Both changes are undone by monkeypatch at teardown, leaving
        # later suites the cache state they expect.
        monkeypatch.syspath_prepend(
            str(Path(ingest_module.__file__).resolve().parent)
        )
        monkeypatch.delitem(sys.modules, "_utils", raising=False)

        # Act
        importlib.reload(ingest_module)

        # Assert: still unset, i.e. the import read no dotenv file.
        for var in POSTGRES_VARS:
            assert var not in os.environ

    def test_main_without_env_file_flag_exits_usage_error(
        self,
        tmp_path: Path,
        mocker: MockerFixture,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A flag-less invocation is rejected by argparse (usage error, exit 2)."""
        # Arrange: argparse rejects before the config is ever opened, so the
        # config path only needs to exist as an argument.
        mocker.patch(
            "sys.argv", ["ingest.py", "--config", str(tmp_path / "any.toml")]
        )

        # Act
        with pytest.raises(SystemExit) as exc:
            main()

        # Assert: argparse's usage error names the missing flag.
        assert exc.value.code == 2
        assert "--env-file" in capsys.readouterr().err
