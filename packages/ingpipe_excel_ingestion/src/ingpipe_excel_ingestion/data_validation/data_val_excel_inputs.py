"""Input validation for the generic Excel ingestion module (pre-ingest, no DB).

For every file in the config: the file exists under ``source_dir``, every
configured ``sheet`` exists in the workbook, parsing the sheet with its
configured (or defaulted) row/column bounds (``header_row`` / ``data_start_row``
/ ``data_end_row`` / ``start_col`` / ``end_col``) succeeds (a valid header row
and at least one data row), and any config-authored ``collection_path`` is a
valid lowercase ltree. Failures accumulate; the script exits 1 if any check
fails.

Usage:
    uv run data-val-excel-inputs \
        --config instances/<instance>/config/ingpipe_excel_ingestion/<name>.toml
"""

import sys
from pathlib import Path
from typing import Any

from ingpipe_lib.cli import (
    build_parser,
    finish_run,
    load_config,
    run_scope,
    setup_entry_logging,
)
from ingpipe_lib.logconfig import get_logger
from ingpipe_lib.paths import InstanceRootNotFoundError, resolve_config_path
from openpyxl.utils.exceptions import InvalidFileException

from ingpipe_excel_ingestion._utils import validate_collection_path
from ingpipe_excel_ingestion.excel_parser import list_sheets, parse_sheet
from ingpipe_excel_ingestion.ingest_excel import validate_config

logger = get_logger(__name__)


def validate_file_exists(source_dir: str, filename: str) -> list[str]:
    """Verify an Excel file exists under the source directory.

    Args:
        source_dir: Directory containing the workbooks.
        filename: Workbook filename.

    Returns:
        A list with one failure message if the file is missing, else empty.
    """
    filepath = Path(source_dir) / filename
    if not filepath.exists():
        return [f"FAIL: File not found: {filepath}"]
    logger.info(f"PASS: File exists: {filepath}")
    return []


def validate_sheets_exist(
    source_dir: str, filename: str, sheet_entries: list[dict[str, Any]]
) -> list[str]:
    """Verify every configured sheet name exists in the workbook.

    Args:
        source_dir: Directory containing the workbooks.
        filename: Workbook filename.
        sheet_entries: The file entry's ``sheets`` list.

    Returns:
        A list of failure messages (empty if all sheets are present).
    """
    filepath = Path(source_dir) / filename
    try:
        actual = set(list_sheets(filepath))
    except (FileNotFoundError, ValueError, InvalidFileException, OSError) as e:
        # list_sheets wraps a corrupt/unreadable workbook into ValueError; record
        # it as a FAIL rather than letting it crash the gate.
        return [f"FAIL: Cannot list sheets in {filename}: {e}"]

    failures: list[str] = []
    for entry in sheet_entries:
        sheet = entry["sheet"]
        if sheet not in actual:
            failures.append(
                f"FAIL: Sheet {sheet!r} not found in {filename}. "
                f"Available: {sorted(actual)}"
            )
        else:
            logger.info(f"PASS: Sheet {sheet!r} found in {filename}")
    return failures


def validate_sheet_data(
    source_dir: str, filename: str, sheet_entries: list[dict[str, Any]]
) -> list[str]:
    """Verify each sheet parses to >=1 column and >=1 data row.

    Args:
        source_dir: Directory containing the workbooks.
        filename: Workbook filename.
        sheet_entries: The file entry's ``sheets`` list.

    Returns:
        A list of failure messages (empty if all sheets yield columns and rows).
    """
    filepath = Path(source_dir) / filename
    failures: list[str] = []

    for entry in sheet_entries:
        sheet = entry["sheet"]
        try:
            columns, rows = parse_sheet(
                filepath,
                sheet,
                header_row=entry.get("header_row"),
                data_start_row=entry.get("data_start_row"),
                data_end_row=entry.get("data_end_row"),
                start_col=entry.get("start_col"),
                end_col=entry.get("end_col"),
            )
        except (FileNotFoundError, ValueError, InvalidFileException) as e:
            failures.append(f"FAIL: Cannot parse {sheet!r} in {filename}: {e}")
            continue

        # parse_sheet raises on a missing/duplicate/blank header, so a successful
        # return always has columns; log the count for visibility.
        logger.info(f"PASS: {sheet!r} in {filename} has {len(columns)} columns")

        if not rows:
            failures.append(f"FAIL: No data rows in {sheet!r} of {filename}")
        else:
            logger.info(f"PASS: {sheet!r} in {filename} has {len(rows)} data rows")

    return failures


def validate_collection_paths(
    filename: str, sheet_entries: list[dict[str, Any]]
) -> list[str]:
    """Verify any config-authored ``collection_path`` is a valid ltree.

    Sheets without an authored ``collection_path`` derive one at ingest time and
    are not checked here. An authored path is validated so a bad one fails before
    the run rather than mid-ingest.

    Args:
        filename: Workbook filename (for messages).
        sheet_entries: The file entry's ``sheets`` list.

    Returns:
        A list of failure messages (empty if all authored paths are valid).
    """
    failures: list[str] = []
    for entry in sheet_entries:
        override = entry.get("collection_path")
        if override is None:
            continue
        try:
            validate_collection_path(override)
        except ValueError as e:
            failures.append(
                f"FAIL: invalid collection_path for {entry['sheet']!r} in "
                f"{filename}: {e}"
            )
        else:
            logger.info(
                f"PASS: authored collection_path {override!r} valid "
                f"({entry['sheet']!r} in {filename})"
            )
    return failures


def main() -> None:
    """Entry point for input validation."""
    parser = build_parser(
        "Validate Excel input files for generic ingestion", env_file=False
    )
    args = parser.parse_args()

    # INFO level, named from the config stem, anchored to the instance root.
    config_path = Path(args.config)
    setup_entry_logging("ingpipe_excel_ingestion/data_validation", config_path)

    with run_scope():
        logger.info("Starting input validation for Excel ingestion")

        # Load the TOML config (exits 1 on a missing config or malformed TOML).
        config = load_config(config_path)

        # Reuse the ingester's canonical config validator as an up-front shape
        # gate so a malformed config (wrong types, missing per-sheet keys) is
        # reported cleanly here rather than crashing a per-field access in the
        # loops below.
        try:
            validate_config(config)
        except ValueError as e:
            logger.error(f"INPUT VALIDATION FAILED: config invalid: {e}")
            sys.exit(1)

        # Anchor a relative source_dir to the instance root, never the CWD.
        try:
            source_dir = str(resolve_config_path(config["source_dir"], config_path))
        except InstanceRootNotFoundError:
            # Already logged with the config path by require_instance_root.
            sys.exit(1)
        files = config["files"]

        all_failures: list[str] = []
        for filename, file_entry in files.items():
            logger.info(f"--- Validating {filename} ---")
            sheet_entries = file_entry["sheets"]

            file_failures = validate_file_exists(source_dir, filename)
            all_failures.extend(file_failures)
            if file_failures:
                # No point listing sheets / parsing a file that does not exist.
                continue

            all_failures.extend(
                validate_sheets_exist(source_dir, filename, sheet_entries)
            )
            all_failures.extend(
                validate_sheet_data(source_dir, filename, sheet_entries)
            )
            all_failures.extend(
                validate_collection_paths(filename, sheet_entries)
            )

        finish_run(
            all_failures,
            success_message="INPUT VALIDATION PASSED: All checks passed",
            failure_prefix="INPUT VALIDATION FAILED",
        )


if __name__ == "__main__":
    main()
