"""Tests for data_val_cleaned_json.validate_cleaned_file."""

import json
import json as _json
from pathlib import Path
from typing import Any

import pytest
from ingpipe_file_ingestion.data_validation import data_val_cleaned_json
from ingpipe_file_ingestion.data_validation.data_val_cleaned_json import validate_cleaned_file


def _make_section(
    sort_order: int,
    heading_text: str = "Heading words here",
    content_text: str = "Some body content text",
    word_count: int | None = None,
    page_start: int = 1,
    page_end: int = 2,
) -> dict[str, Any]:
    """Build a section dict with a self-consistent word_count by default.

    Args:
        sort_order: 1-based section order.
        heading_text: Section heading text.
        content_text: Section body text.
        word_count: Explicit word_count; computed from text when None.
        page_start: First page number.
        page_end: Last page number.

    Returns:
        A section dict matching the cleaned-JSON section shape.
    """
    if word_count is None:
        word_count = len(heading_text.split()) + len(content_text.split())
    return {
        "sort_order": sort_order,
        "heading_text": heading_text,
        "content_text": content_text,
        "word_count": word_count,
        "page_start": page_start,
        "page_end": page_end,
    }


def _write_file(tmp_path: Path, data: Any) -> Path:
    """Write a cleaned-JSON payload to a temp file and return its path.

    Args:
        tmp_path: pytest temp directory.
        data: Top-level JSON payload to serialize.

    Returns:
        Path to the written JSON file.
    """
    json_path = tmp_path / "doc.json"
    json_path.write_text(json.dumps(data), encoding="utf-8")
    return json_path


# A fixed source hash for the document envelope in fixtures.
FIXTURE_BINARY_HASH = 12345678901234567890


def _make_payload(
    n_parsed_sections: int, sections: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build a cleaned-JSON envelope from an explicit count and section list.

    The payload is the two-key envelope: a ``document`` block (carrying
    ``n_parsed_sections`` and a valid ``binary_hash``) and ``sections``.

    Args:
        n_parsed_sections: The document-envelope parsed section count.
        sections: The section dicts.

    Returns:
        A top-level cleaned-JSON payload.
    """
    return {
        "document": {
            "n_parsed_sections": n_parsed_sections,
            "binary_hash": FIXTURE_BINARY_HASH,
        },
        "sections": sections,
    }


def _valid_payload(n_sections: int = 3) -> dict[str, Any]:
    """Build a fully valid cleaned-JSON payload.

    Args:
        n_sections: Number of contiguous sections to generate.

    Returns:
        A valid top-level payload.
    """
    sections = [_make_section(i) for i in range(1, n_sections + 1)]
    return _make_payload(n_sections, sections)


class TestValidateCleanedFileValid:
    """Happy-path tests."""

    def test_validate_cleaned_file_valid_file_passes(self, tmp_path: Path) -> None:
        """A fully valid file produces no failures."""
        json_path = _write_file(tmp_path, _valid_payload(3))

        result = validate_cleaned_file(json_path)

        assert result == []

    def test_validate_cleaned_file_null_content_text_passes(
        self, tmp_path: Path
    ) -> None:
        """A null content_text (heading-only section) is valid (null allowed)."""
        section = _make_section(1)
        section["content_text"] = None
        section["word_count"] = len(section["heading_text"].split())
        json_path = _write_file(tmp_path, _make_payload(1, [section]))

        result = validate_cleaned_file(json_path)

        assert result == []

    def test_validate_cleaned_file_null_pages_pass(self, tmp_path: Path) -> None:
        """Null page_start/page_end are allowed and not flagged."""
        section = _make_section(1, page_start=None, page_end=None)  # type: ignore[arg-type]
        json_path = _write_file(tmp_path, _make_payload(1, [section]))

        result = validate_cleaned_file(json_path)

        assert result == []


class TestValidateCleanedFileFailures:
    """Failure-mode tests.

    Failure messages originate from Pydantic, so assertions do not match the
    exact wording (owned by the schema). Instead each test pins the FAIL string
    to the schema-owned field name (in the ``loc`` for field-level errors, or in
    the custom message for ``model_validator`` invariants), confirming the
    *intended* invariant fired rather than merely that *some* failure was caught.
    """

    def test_validate_cleaned_file_missing_file_reports_failure(self, tmp_path: Path) -> None:
        """A nonexistent file is reported, not raised."""
        result = validate_cleaned_file(tmp_path / "missing.json")

        assert len(result) == 1
        assert "file not found" in result[0]

    def test_validate_cleaned_file_malformed_json_reports_failure(self, tmp_path: Path) -> None:
        """Malformed JSON is reported as exactly one failure, not raised."""
        json_path = tmp_path / "doc.json"
        json_path.write_text("this is not json {{{", encoding="utf-8")

        result = validate_cleaned_file(json_path)

        assert len(result) == 1

    def test_validate_cleaned_file_non_utf8_file_reports_failure(self, tmp_path: Path) -> None:
        """A non-UTF-8 file becomes a FAIL, not a UnicodeDecodeError crash (fix 1.1)."""
        json_path = tmp_path / "doc.json"
        json_path.write_bytes(b"\xff\xfe\x00\x01not valid utf-8 \xff")

        result = validate_cleaned_file(json_path)

        assert len(result) == 1

    def test_validate_cleaned_file_unreadable_path_reports_failure(self, tmp_path: Path) -> None:
        """A path that exists but cannot be read is reported, not raised (OSError branch)."""
        # tmp_path is a directory: exists() is True but read_bytes() raises
        # IsADirectoryError (an OSError), exercising the "could not read" branch.
        result = validate_cleaned_file(tmp_path)

        assert len(result) == 1
        assert "could not read JSON" in result[0]

    def test_validate_cleaned_file_n_parsed_mismatch_caught(self, tmp_path: Path) -> None:
        """n_parsed_sections != len(sections) is flagged."""
        payload = _valid_payload(3)
        payload["document"]["n_parsed_sections"] = 5
        json_path = _write_file(tmp_path, payload)

        result = validate_cleaned_file(json_path)

        assert any("n_parsed_sections" in f for f in result)

    def test_validate_cleaned_file_non_contiguous_sort_order_caught(
        self, tmp_path: Path
    ) -> None:
        """All-int but non-contiguous sort_order is flagged."""
        sections = [_make_section(1), _make_section(2), _make_section(4)]
        json_path = _write_file(tmp_path, _make_payload(3, sections))

        result = validate_cleaned_file(json_path)

        assert any("sort_order" in f for f in result)

    def test_validate_cleaned_file_word_count_mismatch_caught(self, tmp_path: Path) -> None:
        """A word_count that disagrees with the text is flagged."""
        section = _make_section(1, heading_text="one two", content_text="three", word_count=99)
        json_path = _write_file(tmp_path, _make_payload(1, [section]))

        result = validate_cleaned_file(json_path)

        assert any("word_count" in f for f in result)

    def test_validate_cleaned_file_negative_word_count_caught(self, tmp_path: Path) -> None:
        """A negative word_count violates the ge=0 boundary and is flagged."""
        # heading-only section (content_text None) so the only possible failure
        # is the ge=0 boundary, not the "neither heading nor content" invariant.
        section = _make_section(1, heading_text="x", content_text=None, word_count=-1)  # type: ignore[arg-type]
        json_path = _write_file(tmp_path, _make_payload(1, [section]))

        result = validate_cleaned_file(json_path)

        assert any("word_count" in f for f in result)

    def test_validate_cleaned_file_page_start_after_end_caught(self, tmp_path: Path) -> None:
        """page_start > page_end is flagged."""
        section = _make_section(1, page_start=5, page_end=2)
        json_path = _write_file(tmp_path, _make_payload(1, [section]))

        result = validate_cleaned_file(json_path)

        assert any("page_start" in f for f in result)

    def test_validate_cleaned_file_empty_sections_list_caught(self, tmp_path: Path) -> None:
        """An empty sections list is flagged with the explicit zero-sections message.

        The document validator checks non-empty FIRST, so a zero-section payload
        raises the clear "has zero sections" rather than failing as a count
        mismatch (which would be the misleading diagnostic for what is really an
        empty-document problem).
        """
        json_path = _write_file(tmp_path, _make_payload(1, []))

        result = validate_cleaned_file(json_path)

        assert any("has zero sections" in f for f in result)

    def test_validate_cleaned_file_extra_document_key_caught(self, tmp_path: Path) -> None:
        """An unexpected top-level key is rejected (extra='forbid')."""
        payload = _valid_payload(1)
        payload["surprise"] = "unexpected"
        json_path = _write_file(tmp_path, payload)

        result = validate_cleaned_file(json_path)

        assert any("surprise" in f for f in result)

    def test_validate_cleaned_file_extra_section_key_caught(self, tmp_path: Path) -> None:
        """An unexpected section-level key is rejected (extra='forbid')."""
        section = _make_section(1)
        section["surprise"] = "unexpected"
        json_path = _write_file(tmp_path, _make_payload(1, [section]))

        result = validate_cleaned_file(json_path)

        assert any("surprise" in f for f in result)

    def test_validate_cleaned_file_document_missing_binary_hash_caught(
        self, tmp_path: Path
    ) -> None:
        """A document envelope missing binary_hash is flagged.

        The document block requires binary_hash (the source provenance); omitting
        it is a malformed envelope and must be reported, not silently accepted.
        """
        json_path = _write_file(
            tmp_path,
            {
                "document": {"n_parsed_sections": 1},
                "sections": [_make_section(1)],
            },
        )

        result = validate_cleaned_file(json_path)

        assert any("binary_hash" in f for f in result)

    def test_validate_cleaned_file_negative_binary_hash_caught(
        self, tmp_path: Path
    ) -> None:
        """A negative binary_hash violates the ge=0 boundary and is flagged."""
        payload = _valid_payload(1)
        payload["document"]["binary_hash"] = -1
        json_path = _write_file(tmp_path, payload)

        result = validate_cleaned_file(json_path)

        assert any("binary_hash" in f for f in result)

    def test_validate_cleaned_file_extra_document_block_key_caught(
        self, tmp_path: Path
    ) -> None:
        """An unexpected key inside the document block is rejected (extra='forbid')."""
        payload = _valid_payload(1)
        payload["document"]["surprise"] = "unexpected"
        json_path = _write_file(tmp_path, payload)

        result = validate_cleaned_file(json_path)

        assert any("surprise" in f for f in result)

    def test_validate_cleaned_file_missing_sections_key_caught(self, tmp_path: Path) -> None:
        """A payload missing the required 'sections' key is flagged."""
        json_path = _write_file(
            tmp_path,
            {"document": {"n_parsed_sections": 1, "binary_hash": FIXTURE_BINARY_HASH}},
        )

        result = validate_cleaned_file(json_path)

        # "sections" alone is non-discriminating (it is a substring of
        # n_parsed_sections / len(sections)); pin the exact loc + message so the
        # missing-key error is what fired, not some other sections-related check.
        assert any("sections: Field required" in f for f in result)

    def test_validate_cleaned_file_missing_word_count_key_caught(self, tmp_path: Path) -> None:
        """A section missing the required 'word_count' key is flagged."""
        section = _make_section(1)
        del section["word_count"]
        json_path = _write_file(tmp_path, _make_payload(1, [section]))

        result = validate_cleaned_file(json_path)

        assert any("word_count" in f for f in result)


class TestValidateCleanedFileTypeGuards:
    """Type-guard tests: wrong field types are caught, not crash the run."""

    def test_validate_cleaned_file_non_string_content_text_caught(
        self, tmp_path: Path
    ) -> None:
        """A non-null non-string content_text is a failure, not an exception."""
        section = _make_section(1, word_count=5)
        section["content_text"] = 123
        json_path = _write_file(tmp_path, _make_payload(1, [section]))

        result = validate_cleaned_file(json_path)

        assert any("content_text" in f for f in result)

    def test_validate_cleaned_file_non_string_heading_text_caught(
        self, tmp_path: Path
    ) -> None:
        """A non-null non-string heading_text is a failure, not an exception."""
        section = _make_section(1, word_count=5)
        section["heading_text"] = 456
        json_path = _write_file(tmp_path, _make_payload(1, [section]))

        result = validate_cleaned_file(json_path)

        assert any("heading_text" in f for f in result)

    def test_validate_cleaned_file_bool_n_parsed_rejected(self, tmp_path: Path) -> None:
        """A JSON bool for n_parsed_sections (an int field) is rejected."""
        payload = _valid_payload(1)
        payload["document"]["n_parsed_sections"] = True
        json_path = _write_file(tmp_path, payload)

        result = validate_cleaned_file(json_path)

        assert any("n_parsed_sections" in f for f in result)

    def test_validate_cleaned_file_bool_sort_order_rejected(self, tmp_path: Path) -> None:
        """A JSON bool sort_order (an int field) is rejected, not treated as 1."""
        section = _make_section(1)
        section["sort_order"] = True
        json_path = _write_file(tmp_path, _make_payload(1, [section]))

        result = validate_cleaned_file(json_path)

        assert any("sort_order" in f for f in result)

    def test_validate_cleaned_file_non_int_page_start_caught(self, tmp_path: Path) -> None:
        """A non-null non-int page_start is flagged rather than silently skipped."""
        # page_start="1" (not "5") so the only possible failure is the type
        # rejection, not the page_start > page_end ordering invariant.
        section = _make_section(1, page_start="1", page_end=2)  # type: ignore[arg-type]
        json_path = _write_file(tmp_path, _make_payload(1, [section]))

        result = validate_cleaned_file(json_path)

        assert any("page_start" in f for f in result)


# ---------------------------------------------------------------------------
# main() paths
# ---------------------------------------------------------------------------





def _run_main(mocker, config: str) -> None:
    """Invoke main() with the given --config and mocked logging."""
    mocker.patch(
        "ingpipe_file_ingestion.data_validation.data_val_cleaned_json.setup_entry_logging"
    )
    mocker.patch("sys.argv", ["data_val_cleaned_json.py", "--config", config])
    data_val_cleaned_json.main()


def _write_main_config(tmp_path: Path, cleaned_dir: Path) -> str:
    """Write an ingest config naming one document; cleaned_dir absolute."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[module]\n"
        'source_dir = "src"\n'
        "[[module.documents]]\n"
        'file = "a.pdf"\n'
        'title = "A"\n'
        'collection_path = "cms_iom.pub.a"\n'
        "[clean]\n"
        f'cleaned_dir = "{cleaned_dir.as_posix()}"\n',
        encoding="utf-8",
    )
    return str(config_path)


class TestMainPaths:
    """main()'s error and success paths."""

    def test_missing_config_exits_one(self, tmp_path: Path, mocker) -> None:
        with pytest.raises(SystemExit) as exc:
            _run_main(mocker, str(tmp_path / "absent.toml"))
        assert exc.value.code == 1

    def test_malformed_toml_exits_one(self, tmp_path: Path, mocker) -> None:
        config_path = tmp_path / "bad.toml"
        config_path.write_text("not valid = toml ][", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            _run_main(mocker, str(config_path))
        assert exc.value.code == 1

    def test_missing_config_field_exits_one(self, tmp_path: Path, mocker) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text("[module]\n", encoding="utf-8")  # no documents
        with pytest.raises(SystemExit) as exc:
            _run_main(mocker, str(config_path))
        assert exc.value.code == 1

    def test_missing_cleaned_dir_exits_one(self, tmp_path: Path, mocker) -> None:
        """A missing cleaned directory is one clear diagnostic, exit 1."""
        config = _write_main_config(tmp_path, tmp_path / "no-such-dir")
        with pytest.raises(SystemExit) as exc:
            _run_main(mocker, config)
        assert exc.value.code == 1

    def test_empty_document_list_exits_one(self, tmp_path: Path, mocker) -> None:
        cleaned = tmp_path / "cleaned"
        cleaned.mkdir()
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            "[module]\n"
            'source_dir = "src"\n'
            "documents = []\n"
            "[clean]\n"
            f'cleaned_dir = "{cleaned.as_posix()}"\n',
            encoding="utf-8",
        )
        with pytest.raises(SystemExit) as exc:
            _run_main(mocker, str(config_path))
        assert exc.value.code == 1

    def test_validation_failure_exits_one(self, tmp_path: Path, mocker) -> None:
        """An invalid cleaned file makes the run exit 1."""
        cleaned = tmp_path / "cleaned"
        cleaned.mkdir()
        # a.json exists but violates the schema (count mismatch).
        (cleaned / "a.json").write_text(
            _json.dumps(_make_payload(5, [])), encoding="utf-8"
        )
        config = _write_main_config(tmp_path, cleaned)
        with pytest.raises(SystemExit) as exc:
            _run_main(mocker, config)
        assert exc.value.code == 1

    def test_all_files_valid_exits_zero(self, tmp_path: Path, mocker) -> None:
        """A fully valid cleaned set completes without SystemExit."""
        cleaned = tmp_path / "cleaned"
        cleaned.mkdir()
        (cleaned / "a.json").write_text(
            _json.dumps(_valid_payload()), encoding="utf-8"
        )
        config = _write_main_config(tmp_path, cleaned)
        _run_main(mocker, config)
