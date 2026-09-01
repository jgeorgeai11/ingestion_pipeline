"""Unit tests for the data_val_excel_inputs entry point (file-based, no DB).

The four validators run against fixture workbooks written to tmp_path,
asserting each fails on violating input and passes on conforming input;
main() covers the missing-config and malformed-TOML paths plus a full
end-to-end pass/fail pair.
"""

import logging
from pathlib import Path

import pytest
from ingpipe_excel_ingestion.data_validation import data_val_excel_inputs
from ingpipe_excel_ingestion.data_validation.data_val_excel_inputs import (
    validate_collection_paths,
    validate_file_exists,
    validate_sheet_data,
    validate_sheets_exist,
)
from openpyxl import Workbook
from pytest_mock import MockerFixture


@pytest.fixture
def workbook_dir(tmp_path: Path) -> Path:
    """A source dir holding one workbook with a data sheet and an empty sheet."""
    src = tmp_path / "src"
    src.mkdir()
    wb = Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["Code", "Label"])
    ws.append(["A1", "alpha"])
    empty = wb.create_sheet("Empty")
    empty.append(["OnlyHeader"])
    wb.save(src / "wb.xlsx")
    return src


class TestValidateFileExists:
    """validate_file_exists."""

    def test_present_file_passes(self, workbook_dir: Path) -> None:
        assert validate_file_exists(str(workbook_dir), "wb.xlsx") == []

    def test_missing_file_fails(self, workbook_dir: Path) -> None:
        failures = validate_file_exists(str(workbook_dir), "absent.xlsx")
        assert len(failures) == 1
        assert "File not found" in failures[0]


class TestValidateSheetsExist:
    """validate_sheets_exist."""

    def test_present_sheets_pass(self, workbook_dir: Path) -> None:
        failures = validate_sheets_exist(
            str(workbook_dir), "wb.xlsx", [{"sheet": "Data"}, {"sheet": "Empty"}]
        )
        assert failures == []

    def test_missing_sheet_fails_naming_available(self, workbook_dir: Path) -> None:
        failures = validate_sheets_exist(
            str(workbook_dir), "wb.xlsx", [{"sheet": "NoSuch"}]
        )
        assert len(failures) == 1
        assert "NoSuch" in failures[0]
        assert "Data" in failures[0]  # available sheets are listed

    def test_corrupt_workbook_fails_cleanly(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "bad.xlsx").write_bytes(b"not a zip archive")
        failures = validate_sheets_exist(str(src), "bad.xlsx", [{"sheet": "X"}])
        assert len(failures) == 1
        assert "Cannot list sheets" in failures[0]


class TestValidateSheetData:
    """validate_sheet_data."""

    def test_sheet_with_rows_passes(self, workbook_dir: Path) -> None:
        failures = validate_sheet_data(
            str(workbook_dir), "wb.xlsx", [{"sheet": "Data"}]
        )
        assert failures == []

    def test_sheet_without_data_rows_fails(self, workbook_dir: Path) -> None:
        failures = validate_sheet_data(
            str(workbook_dir), "wb.xlsx", [{"sheet": "Empty"}]
        )
        assert len(failures) == 1
        assert "No data rows" in failures[0]

    def test_unparseable_bounds_fail(self, workbook_dir: Path) -> None:
        """An out-of-range header_row surfaces as a parse FAIL, not a crash."""
        failures = validate_sheet_data(
            str(workbook_dir), "wb.xlsx", [{"sheet": "Data", "header_row": 99}]
        )
        assert len(failures) == 1
        assert "Cannot parse" in failures[0]


class TestValidateCollectionPaths:
    """validate_collection_paths."""

    def test_valid_authored_path_passes(self) -> None:
        failures = validate_collection_paths(
            "wb.xlsx", [{"sheet": "Data", "collection_path": "qpp_cm.codes"}]
        )
        assert failures == []

    def test_unauthored_path_not_checked(self) -> None:
        failures = validate_collection_paths("wb.xlsx", [{"sheet": "Data"}])
        assert failures == []

    def test_invalid_authored_path_fails(self) -> None:
        failures = validate_collection_paths(
            "wb.xlsx", [{"sheet": "Data", "collection_path": "Not-Valid"}]
        )
        assert len(failures) == 1
        assert "invalid collection_path" in failures[0]


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, source_dir: Path, sheets_toml: str) -> str:
    """Write a config naming the workbook; source_dir absolute (no instance)."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'source_dir = "{source_dir.as_posix()}"\n'
        'db_name = "policy_db"\n'
        'db_schema = "qpp_cm"\n'
        '[files."wb.xlsx"]\n'
        f"sheets = [\n{sheets_toml}\n]\n",
        encoding="utf-8",
    )
    return str(config_path)


def _run_main(mocker: MockerFixture, config: str) -> None:
    """Invoke main() with the given --config and mocked logging."""
    mocker.patch(
        "ingpipe_excel_ingestion.data_validation.data_val_excel_inputs.setup_entry_logging"
    )
    mocker.patch("sys.argv", ["data_val_excel_inputs.py", "--config", config])
    data_val_excel_inputs.main()


class TestMain:
    """main()'s error paths and end-to-end pass/fail behavior."""

    def test_missing_config_exits_one(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            _run_main(mocker, str(tmp_path / "absent.toml"))
        assert exc.value.code == 1

    def test_malformed_toml_exits_one(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        config_path = tmp_path / "bad.toml"
        config_path.write_text("not valid = toml ][", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            _run_main(mocker, str(config_path))
        assert exc.value.code == 1

    def test_invalid_config_shape_exits_one(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """The ingester's validate_config gate rejects a malformed config."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('source_dir = "x"\n', encoding="utf-8")  # missing keys
        with pytest.raises(SystemExit) as exc:
            _run_main(mocker, str(config_path))
        assert exc.value.code == 1

    def test_all_checks_pass_exits_zero(
        self, tmp_path: Path, workbook_dir: Path, mocker: MockerFixture
    ) -> None:
        config = _write_config(tmp_path, workbook_dir, '  { sheet = "Data" },')
        # No SystemExit: a fully passing input validation returns.
        _run_main(mocker, config)

    def test_failures_exit_one_with_each_logged(
        self,
        tmp_path: Path,
        workbook_dir: Path,
        mocker: MockerFixture,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        config = _write_config(
            tmp_path, workbook_dir,
            '  { sheet = "Data" },\n  { sheet = "NoSuch" },',
        )
        with caplog.at_level(logging.ERROR):
            with pytest.raises(SystemExit) as exc:
                _run_main(mocker, config)
        assert exc.value.code == 1
        assert any("NoSuch" in r.message for r in caplog.records)
