"""Output validation for the clean step's cleaned sections JSON.

Validates the db-ready JSON written by ``ingest.py``'s clean step. Operates
purely on files (no database): it resolves the expected cleaned files from a
config's ``[clean].cleaned_dir`` plus the ``[module].documents`` filenames, then
validates each file against the shared schema.

Validation is delegated wholly to :class:`cleaned_models.CleanedDocument`, which
is the authoritative source of the cleaned-JSON shape and its invariants. The
shape is a two-key envelope: a ``document`` block (the ``n_parsed_sections``
content metric plus the source ``binary_hash`` provenance — Docling's
``origin.binary_hash``) and the ``sections`` records. The delegated checks cover
valid JSON, the envelope structure, field types, the ``n_parsed_sections``/
``sort_order``/``word_count``/page-range checks, the non-negative ``binary_hash``,
and at least one section. See that model for the exact rule list; this module
does not re-enumerate it to avoid drift.

Usage:
    uv run data-val-cleaned-json \
        --config instances/<instance>/config/ingpipe_file_ingestion/<name>.toml
"""

import sys
from pathlib import Path

import pydantic
from ingpipe_lib.cli import (
    build_parser,
    finish_run,
    load_config,
    run_scope,
    setup_entry_logging,
)
from ingpipe_lib.logconfig import get_logger
from ingpipe_lib.paths import InstanceRootNotFoundError, resolve_config_path

# Importing the schema from cleaned_models (NOT from docling_section_parser)
# keeps the validator free of the heavy docling_core dependency.
from ingpipe_file_ingestion.cleaned_models import CleanedDocument

logger = get_logger(__name__)


def validate_cleaned_file(json_path: Path) -> list[str]:
    """Validate one cleaned sections JSON file against the cleaned-doc schema.

    Validation is delegated to :class:`CleanedDocument`, the single source of
    truth shared with the producer. Each Pydantic validation error becomes one
    ``FAIL`` message carrying the field location and the error message.

    Args:
        json_path: Path to a cleaned ``<stem>.json`` file.

    Returns:
        A list of failure messages; empty when every check passes.
    """
    name = json_path.name

    if not json_path.exists():
        return [f"FAIL: {name}: file not found at {json_path}"]

    try:
        raw = json_path.read_bytes()
    except OSError as e:
        return [f"FAIL: {name}: could not read JSON: {e}"]

    # model_validate_json accepts bytes and raises ValidationError for a decode
    # failure (non-UTF-8), malformed JSON, and schema violations alike, so a
    # single except covers decode, parse, and shape errors. Reading bytes (not
    # text) keeps a non-UTF-8 file from raising an uncaught UnicodeDecodeError.
    try:
        document = CleanedDocument.model_validate_json(raw)
    except pydantic.ValidationError as e:
        return [
            # loc is the field path (e.g. "sections.0.word_count"); for
            # model-level invariants it is empty, so fall back to the file name.
            f"FAIL: {name}: {'.'.join(str(p) for p in err['loc']) or '<document>'}: "
            f"{err['msg']}"
            for err in e.errors()
        ]

    logger.info(f"PASS: {name} ({len(document.sections)} sections)")
    return []


def main() -> None:
    """Entry point for cleaned-JSON output validation."""
    # 1. Parse arguments (--config only; this validator needs no database).
    parser = build_parser(
        "Validate the clean step's cleaned sections JSON files", env_file=False
    )
    args = parser.parse_args()

    # 2. Setup logging (after argparse so --help doesn't create log files):
    # INFO level, named from the config stem, anchored to the instance root.
    config_path = Path(args.config)
    setup_entry_logging("ingpipe_file_ingestion/data_validation", config_path)

    with run_scope():
        logger.info("Starting output validation for cleaned sections JSON")

        # 3. Load the TOML config (exits 1 on a missing config or malformed
        # TOML).
        config = load_config(config_path)

        # 4. Resolve cleaned_dir and expected cleaned filenames from config.
        try:
            cleaned_dir = config["clean"]["cleaned_dir"]
            documents = config["module"]["documents"]
            stems = [Path(d["file"]).stem for d in documents]
        except KeyError as e:
            logger.error(f"Missing required config field: {e}")
            sys.exit(1)

        # Anchor a relative cleaned_dir to the instance root, never the CWD.
        try:
            cleaned_dir_path = resolve_config_path(cleaned_dir, config_path)
        except InstanceRootNotFoundError:
            # Already logged with the config path by require_instance_root.
            sys.exit(1)
        if not stems:
            logger.error("Config lists no documents to validate")
            sys.exit(1)
        # Surface a missing cleaned_dir as a single clear diagnostic rather
        # than N separate "file not found" messages, one per expected stem.
        if not cleaned_dir_path.is_dir():
            logger.error(f"Cleaned directory not found: {cleaned_dir_path}")
            sys.exit(1)

        # 5. Validate each expected cleaned file.
        all_failures: list[str] = []
        for stem in stems:
            json_path = cleaned_dir_path / f"{stem}.json"
            logger.info(f"--- Validating {json_path} ---")
            all_failures.extend(validate_cleaned_file(json_path))

        finish_run(
            all_failures,
            success_message=(
                f"OUTPUT VALIDATION PASSED: {len(stems)} file(s) checked"
            ),
            failure_prefix="OUTPUT VALIDATION FAILED",
        )


if __name__ == "__main__":
    main()
