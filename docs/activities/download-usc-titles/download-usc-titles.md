---
name: download-usc-titles
goal: Build a script to download US Code title PDFs (as zip archives) from uscode.house.gov for titles relevant to public health care and social security, with TOML-configurable title selection, dry-run mode, and idempotent downloads.
status: complete
created: 2026-03-17 12:00:00
updated: 2026-03-17 12:00:00
---

## Implementation Plan

1. [completed] Create download script - `code/data_acquisition/usc_titles/download_usc_titles.py`
   - 1.1. CLI entry point using argparse with a single `--config` argument pointing to a TOML config file
   - 1.2. TOML config contains: `base_url`, `release_point`, `output_dir`, `request_delay_seconds`, `dry_run` flag, `verify_ssl` flag, `overwrite` flag, and `titles` list (list of integer title numbers to download)
   - 1.3. Constant dict `TITLE_NAMES` mapping title numbers to descriptive names (e.g., `{5: "Government Organization and Employees", 10: "Armed Forces", ...}`) for logging purposes
   - 1.4. Function `build_download_url(base_url: str, release_point: str, title_number: int) -> str` — construct the PDF zip URL for the given title number and release point, matching the URL pattern on uscode.house.gov (e.g., `https://uscode.house.gov/download/releasepoints/us/pl/119/73not60/pdf_usc05@119-73not60.zip`)
   - 1.5. Function `download_file(url: str, dest: Path, verify_ssl: bool) -> bool` — download a zip file to dest path; skip if file already exists (unless overwrite is enabled); return True if downloaded, False if skipped
   - 1.6. `main()` iterates configured titles, builds URLs, downloads into `{output_dir}/`, logs summary of downloaded/skipped/errored files
   - 1.7. Polite downloading: `time.sleep()` between requests using configurable delay from TOML config
   - 1.8. Logging via project logconfig (`setup_logging`, `get_logger`)

2. [completed] Create TOML config files - `code/data_acquisition/usc_titles/config/download_usc_titles.toml`
   - 2.1. Main config `download_usc_titles.toml`: `base_url = "https://uscode.house.gov"`, `release_point = "119/73not60"`, `output_dir = "data/input/usc_titles"`, `request_delay_seconds = 2.0`, `dry_run = false`, `verify_ssl = false`, `overwrite = false`, `titles = [5, 10, 20, 21, 25, 26, 29, 38, 42]`
   - 2.2. Dry-run config `download_usc_titles_dry_run.toml`: same as main but `dry_run = true` and `titles = [5]`
   - 2.3. Single-title config `download_usc_titles_single.toml`: same as main but `titles = [5]`

3. [completed] Create and run unit tests - `code/data_acquisition/usc_titles/unit_tests/test_download_usc_titles.py`
   - 3.1. Test `build_download_url` generates correct URL for a given title number and release point
   - 3.2. Test `build_download_url` correctly zero-pads single-digit title numbers
   - 3.3. Test `download_file` skips existing files and downloads new ones (mock requests)
   - 3.4. Test `TITLE_NAMES` contains all 9 target titles
   - 3.5. Run tests with pytest and verify all pass

4. [completed] Run download script in dry-run mode - `code/data_acquisition/usc_titles/download_usc_titles.py`
   - 4.1. Run: `uv run code/data_acquisition/usc_titles/download_usc_titles.py --config code/data_acquisition/usc_titles/config/download_usc_titles_dry_run.toml`
   - 4.2. Verify log output shows constructed URL without downloading

5. [completed] Run download script for a single title - `code/data_acquisition/usc_titles/download_usc_titles.py`
   - 5.1. Run: `uv run code/data_acquisition/usc_titles/download_usc_titles.py --config code/data_acquisition/usc_titles/config/download_usc_titles_single.toml`
   - 5.2. Verify zip file downloaded to `data/input/usc_titles/`
   - 5.3. Confirm filename matches expected pattern (e.g., `pdf_usc05@119-73not60.zip`)

6. [completed] Create and run output validation - `code/data_acquisition/usc_titles/data_validation/data_val_downloaded_zips.py`
   - 6.1. Parameters: `output_dir`, `titles` list
   - 6.2. Verify output directory exists and contains expected zip files
   - 6.3. Verify all files are valid zip archives (check with `zipfile.is_zipfile()`)
   - 6.4. Verify no zero-byte files
   - 6.5. Verify each zip contains at least one PDF file (check filenames inside zip)
   - 6.6. Log file count, individual file sizes, and total size
   - 6.7. Run validation on `data/input/usc_titles/` for title 5

## Key Data Decisions and Considerations

1. **Download zips, not individual PDFs** — The site provides titles as zip archives containing PDFs; the script downloads the zip as-is rather than extracting, to preserve the original packaging and allow downstream processing to handle extraction
2. **Configurable release point** — The release point (e.g., `119/73not60`) changes as new public laws are enacted; making it a config value avoids code changes for each update
3. **SSL verification disabled by default** — The uscode.house.gov site has SSL certificate issues (`unable to get local issuer certificate`); the `verify_ssl` config flag allows toggling this
4. **Idempotent downloads** — Skip files that already exist on disk (unless `overwrite = true`), so the script can be re-run safely after interruptions or to add new titles
5. **Overwrite flag** — When `overwrite = true`, delete and re-download existing zip files; useful when a new release point updates the same title content
6. **Known URL pattern** — URLs follow a deterministic pattern based on title number and release point, so no scraping/parsing of the download page is needed
7. **Packages** — `requests` is already installed and on the approved packages list; no new dependencies needed

## Status & Next Steps

**Current Status**: Complete
**Completed**:
1. Activity plan created
2. Download script implemented with CLI, TOML config, dry-run mode, idempotent downloads, and overwrite support
3. Three TOML configs created (full, dry-run, single-title)
4. Unit tests created and passing (17/17)
5. Dry-run mode verified (logs URLs without downloading)
6. Single-title download verified (Title 5: pdf_usc05@119-73not60.zip, 5,069,070 bytes)
7. Data validation script created and passing for Title 5
**Next Steps**:
1. Run full download with all 9 titles when ready (use download_usc_titles.toml config)
2. Run data validation after full download to verify all 9 zip files
**Blockers**:
1. None
**Notes**:
1. Target titles: 5 (FEHBP), 10 (Armed Forces/TRICARE), 20 (Education), 21 (Food and Drugs/FDA), 25 (Indians/IHS), 26 (Internal Revenue), 29 (Labor/ERISA), 38 (Veterans' Benefits), 42 (Public Health and Welfare/Medicare/Medicaid/SSA)
2. Current release point is through Public Law 119-73 (01/23/2026), except 119-60
3. Some zip files may be large (Title 42 in particular covers all of Medicare, Medicaid, and Social Security)
4. SSL verification is disabled by default due to certificate issues on uscode.house.gov
