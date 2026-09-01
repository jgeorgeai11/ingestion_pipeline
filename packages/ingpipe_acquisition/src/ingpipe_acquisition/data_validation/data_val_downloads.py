"""Validate an acquired corpus against the manifest its run wrote.

This is the check no previous acquisition validator could make, because
nothing recorded what a run intended to produce. The two it replaces both
answered a weaker question and answered it wrongly:

  - ``data_val_downloaded_pdfs.py`` passed a manual folder that contained *at
    least one* PDF, so a manual with 1 of 40 chapters passed. Its header check
    read four bytes and its size check only rejected zero -- both of which a
    truncated file satisfies.
  - ``data_val_downloaded_zips.py`` asserted the presence of zip archives that
    the downloader deletes after extraction, so it failed after every
    successful run.

Here the manifest is the specification: every recorded artifact must exist, be
non-empty, and match its recorded byte size, and an entry the run recorded as
FAILED is a validation failure rather than a check that is quietly skipped. A
missing or malformed manifest fails too -- a corpus nothing claims to have
produced cannot be reported as valid.

Usage:
    uv run data-val-downloads \\
        --config instances/policy_db/config/ingpipe_acquisition/usc_titles/data_val_downloads.toml
"""

import sys
import zipfile
from pathlib import Path

from ingpipe_lib.cli import build_parser, finish_run, load_config, run_scope, setup_entry_logging
from ingpipe_lib.logconfig import get_logger
from ingpipe_lib.paths import InstanceRootNotFoundError, resolve_config_path

from ingpipe_acquisition.manifest import STATUS_FAILED, ManifestEntry, ManifestError, read_manifest

__all__ = ["main", "validate_downloads"]

logger = get_logger(__name__)

LOG_SUBDIR = "ingpipe_acquisition/data_validation"

# The content checks a config may name per file extension. Each answers "is
# this the KIND of file it claims to be", which a size check cannot: a
# truncated PDF has the right size only by coincidence, but it fails to be a
# PDF only if something reads more than its first four bytes.
CONTENT_CHECKS = ("pdf", "zip")

# PDF files begin with this magic byte sequence.
PDF_HEADER = b"%PDF"


def check_pdf(path: Path) -> str | None:
    """Check that a file is a PDF.

    Args:
        path: The artifact to check.

    Returns:
        A failure message, or None when the file is a PDF.
    """
    try:
        with open(path, "rb") as handle:
            header = handle.read(len(PDF_HEADER))
    except OSError as e:
        return f"{path}: could not be read for the PDF check ({e})"
    if header != PDF_HEADER:
        return f"{path}: does not begin with {PDF_HEADER.decode()} (got {header!r})"
    return None


def check_zip(path: Path) -> str | None:
    """Check that a file is a readable zip archive.

    The central directory lives at the END of a zip, so reading it is a real
    completeness check: a truncated archive fails here even though its first
    bytes are a valid zip signature.

    Args:
        path: The artifact to check.

    Returns:
        A failure message, or None when the archive reads cleanly.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            if not zf.namelist():
                return f"{path}: is a valid zip but contains no members"
    except (zipfile.BadZipFile, OSError) as e:
        return f"{path}: is not a readable zip archive ({e})"
    return None


_CHECKS = {"pdf": check_pdf, "zip": check_zip}


def validate_content_checks(config: dict) -> dict[str, str]:
    """Read and validate the optional per-extension content checks.

    Args:
        config: The parsed TOML config.

    Returns:
        A mapping of lowercase file extension (with the leading dot) to check
        name, empty when the config declares none.

    Raises:
        ValueError: If the table is malformed or names an unknown check.
    """
    raw = config.get("content_checks", {})
    if not isinstance(raw, dict):
        raise ValueError(f"Config table '[content_checks]' must be a table, got {raw!r}")

    checks: dict[str, str] = {}
    for extension, name in raw.items():
        if not isinstance(name, str) or name not in CONTENT_CHECKS:
            raise ValueError(
                f"content_checks['{extension}'] must be one of {list(CONTENT_CHECKS)}, "
                f"got {name!r}"
            )
        if not extension.startswith("."):
            raise ValueError(
                f"content_checks key {extension!r} must be a file extension including "
                "its leading dot (e.g. '.pdf')"
            )
        checks[extension.lower()] = name
    return checks


def _validate_entry(
    entry: ManifestEntry, output_root: Path, content_checks: dict[str, str]
) -> list[str]:
    """Validate one manifest entry's artifacts, returning its failures.

    Args:
        entry: The recorded entry.
        output_root: The corpus root the entry's paths are relative to.
        content_checks: The extension-to-check mapping from the config.

    Returns:
        The failure messages for this entry; empty when it validates.
    """
    if entry.status == STATUS_FAILED:
        # The run that wrote this manifest already knew this target did not
        # arrive. Reporting it here is what turns a partial corpus into a
        # failed validation instead of a passing one with a smaller set.
        return [f"{entry.url}: recorded as FAILED by the acquisition run ({entry.error})"]

    if not entry.artifacts:
        return [f"{entry.url}: recorded no artifacts, so nothing can be verified"]

    failures: list[str] = []
    # Every message below renders the manifest-relative path with as_posix()
    # rather than interpolating the Path: str() follows the host, so the same
    # corpus reported "manual\b.pdf" on Windows and "manual/b.pdf" on macOS.
    # POSIX form is also how the manifest already STORES these paths
    # (manifest.py writes as_posix() for destination and each artifact path),
    # so this makes the message quote the file rather than paraphrase it.
    for artifact in entry.artifacts:
        path = output_root / artifact.path
        if not path.is_file():
            failures.append(f"{artifact.path.as_posix()}: missing (recorded by {entry.url})")
            continue

        size = path.stat().st_size
        if size == 0:
            failures.append(f"{artifact.path.as_posix()}: is empty (recorded by {entry.url})")
            continue
        if size != artifact.size:
            failures.append(
                f"{artifact.path.as_posix()}: is {size:,} bytes but the run recorded "
                f"{artifact.size:,} (recorded by {entry.url})"
            )
            continue

        check_name = content_checks.get(path.suffix.lower())
        if check_name is not None:
            message = _CHECKS[check_name](path)
            if message is not None:
                failures.append(message)
    return failures


def validate_downloads(config: dict, config_path: str | Path) -> list[str]:
    """Validate a corpus against the manifest at its output root.

    Args:
        config: The parsed TOML config. Requires ``output_dir``; may declare
            ``content_checks``.
        config_path: The ``--config`` path, used to anchor a relative
            ``output_dir`` to the instance root.

    Returns:
        One message per failed check; empty when the corpus matches its
        manifest exactly.

    Raises:
        ValueError: If ``output_dir`` is missing or the content-check table is
            malformed.
        ManifestError: If the manifest is absent or unreadable. A corpus with
            no manifest cannot pass vacuously.
        InstanceRootNotFoundError: If a relative ``output_dir`` has no instance
            root to anchor to.
    """
    output_dir = config.get("output_dir")
    if not isinstance(output_dir, str) or not output_dir:
        raise ValueError("Config key 'output_dir' is required and must be a non-empty string")

    content_checks = validate_content_checks(config)
    output_root = resolve_config_path(output_dir, config_path)
    logger.info(f"Validating downloads under {output_root}")

    entries = read_manifest(output_root)
    logger.info(f"Manifest records {len(entries)} target(s)")

    failures: list[str] = []
    artifact_count = 0
    for entry in entries:
        artifact_count += len(entry.artifacts)
        failures.extend(_validate_entry(entry, output_root, content_checks))

    for failure in failures:
        logger.error(f"Validation failure: {failure}")
    logger.info(
        f"Checked {artifact_count} artifact(s) across {len(entries)} manifest "
        f"entry(ies): {len(failures)} failure(s)"
    )
    return failures


def main() -> None:
    """Entry point: validate one acquired corpus and exit non-zero on failure.

    Raises:
        SystemExit: With code 1 when the config is invalid, the manifest is
            missing or unreadable, or any recorded artifact fails a check.
    """
    parser = build_parser(
        "Validate an acquired corpus against the manifest its acquisition run "
        "wrote: every recorded artifact must exist at its recorded size, and no "
        "target may be recorded as failed.",
        env_file=False,
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    setup_entry_logging(LOG_SUBDIR, config_path)

    with run_scope():
        config = load_config(config_path)

        try:
            failures = validate_downloads(config, config_path)
        except InstanceRootNotFoundError:
            # Already logged with the config path by require_instance_root.
            sys.exit(1)
        except (ManifestError, ValueError) as e:
            logger.error(f"DOWNLOAD VALIDATION FAILED: {e}")
            sys.exit(1)

        finish_run(
            failures,
            success_message=(
                f"DOWNLOAD VALIDATION PASSED: every artifact recorded for "
                f"{config_path.stem} is present at its recorded size"
            ),
            failure_prefix="DOWNLOAD VALIDATION FAILED",
        )


if __name__ == "__main__":
    main()
