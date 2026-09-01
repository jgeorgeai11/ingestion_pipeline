"""Ingest documents into PostgreSQL for RAG retrieval.

Three-step pipeline controlled by TOML config flags:
  1. parse — Convert source files to Docling JSON (output to parsed_dir)
  2. clean — Parse the Docling JSON into cleaned, db-ready sections JSON (output to cleaned_dir)
  3. load  — Insert documents and sections into PostgreSQL

Each step can be enabled/disabled independently via the run flag in its
config section ([parse], [clean], [load]). All default to true.

Output filenames match the original source filename's stem throughout the
pipeline (e.g., ge101c01.pdf → ge101c01.json in both parsed_dir and cleaned_dir).

The load step consumes the clean step's db-ready JSON (validated via the shared
CleanedDocument Pydantic model) and inserts it into the collection_path-keyed
document/document_content tables. Each document's identity is its authored
collection_path (an ltree). The collection_path is validated up front: a
document whose collection_path is missing, blank, or not a valid lowercase
ltree is skipped (logged as a warning) and excluded from the entire run. A
config in which two documents share an identity — the same file stem (their
artifacts would collide) or the same collection_path (their document rows would
collide) — is a config error and aborts the run before any step executes.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pydantic
from ingpipe_lib.cli import (
    build_parser,
    finish_run,
    load_config,
    run_scope,
    setup_entry_logging,
)
from ingpipe_lib.db import get_engine, require_extensions
from ingpipe_lib.logconfig import get_logger
from ingpipe_lib.paths import InstanceRootNotFoundError, resolve_config_path
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from ingpipe_file_ingestion._utils import (
    ensure_schema,
    validate_collection_path,
    validate_sql_identifier,
)
from ingpipe_file_ingestion.cleaned_models import CleanedDocument
from ingpipe_file_ingestion.docling_section_parser import parse_docling_json, sections_to_record
from ingpipe_file_ingestion.file_parser import FORMAT_CONFIG, parse_files_docling

logger = get_logger(__name__)

# Hard defaults for per-file parse settings, used when a document entry and the
# [parse] section both omit the setting. Single source of truth within this
# module so main() and _group_files_by_parse_settings cannot drift apart.
DEFAULT_DO_OCR = False
# dlparse (DoclingParseDocumentBackend) is docling's canonical PDF backend.
# dlparse_v2 / dlparse_v4 are deprecated subclasses of it (warn + will raise in a
# future release), so they are not used. docling-parse's per-page virtual-memory
# over-commit on large PDFs is bounded by DEFAULT_MAX_PAGES_PER_BATCH (page-range
# batching), not by the backend choice.
DEFAULT_PDF_BACKEND = "dlparse"
# A PDF with more pages than this is parsed in page-range slices and stitched
# back (DoclingDocument.concatenate) to bound the docling-parse backend's
# per-page memory, which otherwise over-commits and OOMs/crashes large parses.
# Batching is lossless vs single-pass; 0 disables it.
DEFAULT_MAX_PAGES_PER_BATCH = 25

# Default names of the document tables. Configurable per config via the
# [load].document_table / content_table keys; defined once here so main(),
# step_load, and the output validator (data_val_loaded_documents) cannot
# drift on the defaults.
DOCUMENT_TABLE = "document"
DOCUMENT_CONTENT_TABLE = "document_content"


def filter_valid_documents(
    documents: list[dict[str, object]],
) -> tuple[list[dict[str, object]], dict[str, str]]:
    """Drop documents with an invalid ``collection_path`` and return the rest.

    Colliding ``file`` values across the input are a config error and fail fast:
    every per-document map in :func:`main` is keyed on ``file`` (so a repeated
    filename would silently last-wins-overwrite), and every parsed/cleaned
    artifact is named from ``Path(file).stem`` (so two entries sharing a stem,
    e.g. ``a.pdf`` and ``a.docx``, would overwrite each other's outputs and load
    the same content twice). The check therefore keys on the stem — the artifact
    key that actually collides — and runs across ALL input documents before any
    ``collection_path`` filtering, since a colliding filename is a config error
    regardless of whether either copy has a valid ``collection_path``.

    Each remaining document's ``collection_path`` is validated with
    :func:`validate_collection_path`. A document whose ``collection_path`` is
    missing, blank, or not a valid lowercase ``ltree`` is logged as a warning
    and excluded; the kept documents are returned together with their
    (validated, unchanged) collection_path keyed by source filename.

    A repeated ``collection_path`` among the KEPT documents is likewise a config
    error and fails fast: ``collection_path`` is the ``document`` primary key, so
    the second entry would either be reported as "already ingested" (and counted
    as a success while never being ingested) or, under ``overwrite``, replace the
    first — silently dropping one document either way.

    Args:
        documents: The ``[module].documents`` entries from the TOML config.

    Returns:
        A tuple of (kept documents, mapping of source filename to its validated
        ``collection_path``). The kept documents preserve their original order.

    Raises:
        ValueError: If two or more input documents share the same ``file``
            value or the same ``Path(file).stem``, or if two or more kept
            documents share the same ``collection_path`` (config errors; they
            propagate to main()'s config-parse handler).
    """
    # Detect colliding filenames across the full input before filtering: the
    # per-document maps in main() are keyed on `file`, and every parsed/cleaned
    # artifact is keyed on Path(file).stem, so a repeated filename collapses the
    # maps and a shared stem collides on disk. The stem is the stricter key (it
    # catches both), so group by it and fail fast naming every offending file.
    stem_files: dict[str, list[str]] = {}
    for document in documents:
        file_name = str(document["file"])
        stem_files.setdefault(Path(file_name).stem, []).append(file_name)
    dupes = {
        name for names in stem_files.values() if len(names) > 1 for name in names
    }
    if dupes:
        raise ValueError(f"Duplicate document file(s) in config: {sorted(dupes)}")

    kept: list[dict[str, object]] = []
    collection_paths: dict[str, str] = {}
    # Repeated collection_paths are collected across the whole (kept) input and
    # reported together, matching the filename check's name-every-offender style.
    # Only kept documents participate: a document dropped for an invalid path is
    # excluded from the run, so it cannot collide with anything.
    seen_paths: set[str] = set()
    path_dupes: set[str] = set()
    for document in documents:
        file_name = str(document["file"])
        raw_path = document.get("collection_path")
        # Guard for a missing/blank path before the regex: passing None to the
        # validator would raise TypeError, and a blank string is invalid anyway.
        if not raw_path or not str(raw_path).strip():
            logger.warning(
                f"Skipping {file_name}: invalid collection_path {raw_path!r} "
                "(missing or blank)"
            )
            continue
        try:
            valid_path = validate_collection_path(str(raw_path))
        except ValueError as e:
            logger.warning(
                f"Skipping {file_name}: invalid collection_path {raw_path!r} ({e})"
            )
            continue
        kept.append(document)
        if valid_path in seen_paths:
            path_dupes.add(valid_path)
        seen_paths.add(valid_path)
        collection_paths[file_name] = valid_path
    if path_dupes:
        raise ValueError(
            f"Duplicate collection_path(s) in config: {sorted(path_dupes)}"
        )
    return kept, collection_paths


# ---------------------------------------------------------------------------
# Step 1: Parse — Convert source files via Docling
# ---------------------------------------------------------------------------

def _group_files_by_parse_settings(
    file_paths: list[str],
    do_ocr_map: dict[str, bool],
    pdf_backend_map: dict[str, str],
) -> dict[tuple[bool, str], list[str]]:
    """Group files by their resolved (do_ocr, pdf_backend) settings.

    Files that share the same settings can be parsed with a single Docling
    converter. The returned mapping preserves the first-seen order of both the
    groups and the files within them. Files absent from a map fall back to the
    hard defaults (DEFAULT_DO_OCR, DEFAULT_PDF_BACKEND).

    Args:
        file_paths: Filenames to group.
        do_ocr_map: Per-file do_ocr setting.
        pdf_backend_map: Per-file pdf_backend setting.

    Returns:
        Ordered mapping of (do_ocr, pdf_backend) to the files using it.
    """
    groups: dict[tuple[bool, str], list[str]] = {}
    for file_name in file_paths:
        key = (
            do_ocr_map.get(file_name, DEFAULT_DO_OCR),
            pdf_backend_map.get(file_name, DEFAULT_PDF_BACKEND),
        )
        groups.setdefault(key, []).append(file_name)
    return groups


def step_parse(
    source_dir: str,
    file_paths: list[str],
    parsed_dir: str,
    overwrite: bool = False,
    *,
    do_ocr_map: dict[str, bool] | None = None,
    pdf_backend_map: dict[str, str] | None = None,
    max_pages_per_batch: int = 0,
) -> tuple[list[str], list[dict[str, str]]]:
    """Convert source files with Docling to Docling JSON.

    JSON is the only output: it is the sole format the clean step consumes, so
    the ingest pipeline always parses to JSON (the generic, multi-format
    capability lives in ``file_parser.parse_files_docling``).

    Parsing settings (do_ocr, pdf_backend) are resolved per file from the
    supplied maps. Files sharing the same settings are parsed together with a
    single Docling converter; a mixed batch builds one converter per distinct
    settings combination.

    Each settings group is parsed independently. A ``RuntimeError`` from one
    group (files failing to convert or export) is caught and recorded rather
    than aborting later groups. After every group has been attempted, the
    surviving files are determined by inspecting the parsed JSON on disk: a
    file whose ``parsed_dir/<stem>.json`` output exists and is non-empty is a
    survivor (this also captures the skip-if-exists case, since a previously
    parsed file already has its JSON on disk). Every input file NOT among the
    survivors is recorded as a parse failure. A ``ValueError`` (invalid output
    format or pdf_backend) is a config error and propagates immediately; it is
    not recorded as a per-file failure.

    Args:
        source_dir: Directory containing input files.
        file_paths: Filenames relative to source_dir.
        parsed_dir: Directory for parsed output files.
        overwrite: If False, skip a file whose parsed JSON already exists and
            is non-empty.
        do_ocr_map: Per-file do_ocr setting. Files absent default to
            DEFAULT_DO_OCR.
        pdf_backend_map: Per-file pdf_backend setting. Files absent default
            to DEFAULT_PDF_BACKEND.
        max_pages_per_batch: Run-level page-batch size passed to
            parse_files_docling; a PDF larger than this is parsed in page-range
            slices and stitched back. 0 disables batching.

    Returns:
        A tuple of (ok_files, failures). ``ok_files`` is the subset of the
        input files whose parsed JSON output exists and is non-empty.
        ``failures`` is a list of dicts (one per file not parsed), each with
        keys ``file``, ``stage`` ("parse"), and ``reason``.

    Raises:
        ValueError: If a pdf_backend is invalid (a config error, not a per-file
            failure).
    """
    do_ocr_map = do_ocr_map or {}
    pdf_backend_map = pdf_backend_map or {}

    # Preserve the full input list before the skip filter rebinds file_paths to
    # only the files that need converting. The survivor (parsed_ok) computation
    # runs over this original list so already-parsed (skipped) files still count
    # as survivors.
    input_files = list(file_paths)

    logger.info(f"Step 1 (parse): Converting {len(file_paths)} files with Docling (overwrite={overwrite})")

    parsed_dir_path = Path(parsed_dir)
    json_ext = FORMAT_CONFIG["json"]["ext"]
    if not overwrite:
        # Skip a file whose parsed JSON already exists and is non-empty — the
        # clean step's only input. parse_files_docling writes each output
        # atomically, so a present, non-empty .json is complete.
        to_convert = []
        for file_name in file_paths:
            json_path = parsed_dir_path / (Path(file_name).stem + json_ext)
            if json_path.exists() and json_path.stat().st_size > 0:
                logger.info(f"Skipping {file_name}: parsed JSON already exists")
            else:
                to_convert.append(file_name)
        # Fall through (rather than early-return) when nothing needs converting,
        # so parsed_ok/failures are still computed from the JSON on disk.
        file_paths = to_convert
    else:
        # Overwrite: delete any stale parsed JSON before re-converting. The
        # survivor check below reads the JSON on disk, so a leftover from a prior
        # run would otherwise mask a failed re-conversion as a success.
        for file_name in file_paths:
            (parsed_dir_path / (Path(file_name).stem + json_ext)).unlink(missing_ok=True)

    # Parse files grouped by their per-file settings so each Docling converter
    # is built once per distinct (do_ocr, pdf_backend) combination.
    groups = _group_files_by_parse_settings(file_paths, do_ocr_map, pdf_backend_map)
    # Attempt every group so one bad group does not prevent unrelated later groups
    # from being parsed; survivors are determined from the JSON on disk after all
    # groups are attempted. ValueError (invalid format/backend) is a config error
    # and is not caught — it propagates. Each failing group's error is recorded
    # against its own files so failure attribution is per-group, not shared.
    file_reason: dict[str, str] = {}
    for (group_do_ocr, group_backend), group_files in groups.items():
        logger.info(
            f"Parsing {len(group_files)} file(s) with do_ocr={group_do_ocr}, pdf_backend={group_backend}"
        )
        try:
            parse_files_docling(
                source_dir, group_files, parsed_dir, ["json"],
                do_ocr=group_do_ocr, pdf_backend=group_backend,
                max_pages_per_batch=max_pages_per_batch,
            )
        except RuntimeError as e:
            logger.error(
                f"Group (do_ocr={group_do_ocr}, pdf_backend={group_backend}) had failures: {e}"
            )
            for group_file in group_files:
                file_reason[group_file] = str(e)

    # The clean step consumes the parsed JSON, so a file is a survivor only when
    # its JSON output exists and is non-empty on disk (skipped files already have
    # it). Every input file without a usable JSON is a recorded parse failure,
    # attributed to its own group's error where one occurred.
    parsed_ok: list[str] = []
    failures: list[dict[str, str]] = []
    for file_name in input_files:
        json_path = parsed_dir_path / (Path(file_name).stem + json_ext)
        if json_path.exists() and json_path.stat().st_size > 0:
            parsed_ok.append(file_name)
        else:
            reason = file_reason.get(file_name, "parsed JSON output missing or empty")
            failures.append({"file": file_name, "stage": "parse", "reason": reason})

    logger.info(
        f"Step 1 (parse): Complete — {len(parsed_ok)}/{len(input_files)} files have parsed JSON in {parsed_dir}"
    )
    return parsed_ok, failures


# ---------------------------------------------------------------------------
# Step 2: Clean — Parse Docling JSON into cleaned, db-ready sections JSON
# ---------------------------------------------------------------------------

def _write_json_atomic(out_path: Path, record: object, out_dir_path: Path) -> None:
    """Serialise ``record`` as JSON to ``out_path`` atomically.

    Writes to a temp file in ``out_dir_path`` (same filesystem as the target, so
    ``os.replace`` is atomic), then renames it into place. Mirrors
    ``file_parser._export_atomic``: the skip sentinel in :func:`step_clean`
    treats an existing non-empty output as complete, so a crash or disk-full
    mid-write must never leave a truncated file at ``out_path``.

    Args:
        out_path: Final destination path for the JSON output.
        record: JSON-serialisable payload to write.
        out_dir_path: Directory used for the temp file (must contain out_path).

    Raises:
        OSError: If the temp file cannot be created, written, or renamed.
        TypeError: If ``record`` is not JSON-serialisable.
    """
    # A neutral ".tmp" suffix keeps a crash-orphan unambiguous against any
    # glob-based scan of the output directory rather than masquerading as a real
    # output (".json"), which the skip sentinel would then honour.
    fd, tmp_name = tempfile.mkstemp(
        dir=str(out_dir_path), prefix=f".{out_path.stem}.", suffix=".tmp"
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        tmp_path.write_text(
            json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(tmp_path, out_path)
    except Exception:
        # Clean up the temp file on any failure so it never lingers
        tmp_path.unlink(missing_ok=True)
        raise


def step_clean(
    parsed_dir: str,
    cleaned_dir: str,
    file_paths: list[str],
    overwrite: bool = False,
) -> tuple[list[str], list[dict[str, str]]]:
    """Clean each parsed Docling JSON into a db-ready sections JSON.

    For each file, reads ``parsed_dir/<stem>.json``, parses it into cleaned
    sections (deterministic, generic cleaning), and writes the identity-agnostic
    payload to ``cleaned_dir/<stem>.json``. Output filenames match the original
    source filename's stem (e.g., ge101c01.json). Each output is written
    atomically (temp file then ``os.replace``), so an interrupted write never
    leaves a truncated file for the skip sentinel to accept as complete.

    The step is resilient: a file that fails to clean (missing parsed JSON,
    malformed Docling document, zero-section parse, or an IO error) is recorded
    as a failure and dropped, and its siblings continue. A file that is cleaned
    OR skipped-because-already-present is a survivor.

    Args:
        parsed_dir: Directory containing the Docling JSON files from step 1.
        cleaned_dir: Directory for the cleaned sections JSON output.
        file_paths: Source filenames (used to derive the JSON filenames).
        overwrite: If False, skip a file whose non-empty cleaned output already
            exists.

    Returns:
        A tuple of (ok_files, failures). ``ok_files`` is the subset of the input
        files that were cleaned or already present. ``failures`` is a list of
        dicts (one per file that failed), each with keys ``file``, ``stage``
        ("clean"), and ``reason``.

    Raises:
        Exception: Only an unexpected exception type (a genuine bug) propagates.
            The expected per-file failures (``FileNotFoundError``, ``OSError``,
            ``ValueError``) are caught and recorded, not raised.
    """
    logger.info(f"Step 2 (clean): Processing {len(file_paths)} Docling JSON files (overwrite={overwrite})")
    parsed_dir_path = Path(parsed_dir)
    cleaned_dir_path = Path(cleaned_dir)
    cleaned_dir_path.mkdir(parents=True, exist_ok=True)

    cleaned_ok: list[str] = []
    failures: list[dict[str, str]] = []
    for file_name in file_paths:
        json_filename = Path(file_name).stem + ".json"
        cleaned_json_path = cleaned_dir_path / json_filename

        # Skip a file whose cleaned JSON already exists and is non-empty. The
        # write below is atomic (_write_json_atomic), so a present, non-empty
        # output is complete and cannot be a truncated partial write.
        if (
            not overwrite
            and cleaned_json_path.exists()
            and cleaned_json_path.stat().st_size > 0
        ):
            logger.info(f"Skipping {json_filename}: already exists in {cleaned_dir}")
            cleaned_ok.append(file_name)
            continue

        # Each file is cleaned independently. A per-file failure (missing input,
        # malformed JSON, zero sections, or IO error) is recorded and the file
        # dropped so its siblings still clean. Any OTHER exception type (a
        # genuine bug) propagates to main()'s pipeline handler.
        try:
            parsed_json_path = parsed_dir_path / json_filename
            if not parsed_json_path.exists():
                raise FileNotFoundError(
                    f"Parsed JSON not found in {parsed_dir}: {json_filename}"
                )

            logger.info(f"Cleaning {json_filename}")
            # parse_docling_json returns the cleaned sections and the source
            # provenance hash (Docling's origin.binary_hash) read from the parsed
            # output; the hash is threaded into the cleaned record's document
            # envelope. No source-file access happens here.
            sections, binary_hash = parse_docling_json(parsed_json_path)
            # A zero-section parse is an error: do not write the empty record
            # (which would otherwise become a "sticky" satisfied output for the
            # skip sentinel). Record it as a failure, aligning the producer with
            # the validator, which rejects zero-section files.
            if not sections:
                raise ValueError(f"No sections parsed from {json_filename}")

            record = sections_to_record(sections, binary_hash)
            _write_json_atomic(cleaned_json_path, record, cleaned_dir_path)
            logger.info(
                f"Wrote cleaned JSON: {cleaned_json_path} ({len(sections)} sections)"
            )
            cleaned_ok.append(file_name)
        except (FileNotFoundError, OSError, ValueError) as e:
            logger.warning(f"Clean failed for {file_name}: {e}")
            failures.append({"file": file_name, "stage": "clean", "reason": str(e)})
            continue

    logger.info(f"Step 2 (clean): Complete — {len(cleaned_ok)}/{len(file_paths)} files cleaned")
    return cleaned_ok, failures


# ---------------------------------------------------------------------------
# Step 3: Load — Insert documents and sections into PostgreSQL
# ---------------------------------------------------------------------------

def step_load(
    cleaned_dir: str,
    file_paths: list[str],
    engine: Engine,
    db_schema: str,
    overwrite: bool,
    *,
    collection_paths: dict[str, str],
    document_titles: dict[str, str],
    document_table: str = DOCUMENT_TABLE,
    content_table: str = DOCUMENT_CONTENT_TABLE,
) -> tuple[list[str], list[dict[str, str]]]:
    """Load cleaned-JSON documents and their sections into the database.

    For each source file, reads ``cleaned_dir/<stem>.json``, validates and
    parses it with the shared :class:`CleanedDocument` model, then inserts one
    document row (keyed by the file's validated ``collection_path``, carrying the
    document-envelope ``n_parsed_sections`` and the source ``binary_hash``
    provenance as ``source_binary_hash``) and one content row per section — all
    within a single transaction per document. No source file is read at load;
    the hash comes from the validated cleaned JSON.

    Re-ingestion keys on ``collection_path`` alone: an existing document is
    skipped unless ``overwrite`` is set, in which case it is deleted first (its
    content cascades) and re-inserted.

    Args:
        cleaned_dir: Directory containing the cleaned ``<stem>.json`` files.
        file_paths: Source filenames (used to derive the JSON filenames and to
            key the per-file ``collection_paths``/``document_titles`` maps).
        engine: SQLAlchemy Engine connected to the target database.
        db_schema: PostgreSQL schema name.
        overwrite: If True, delete an existing document and re-ingest it.
        collection_paths: Mapping of source filename to its validated
            ``collection_path`` (the document primary key, an ltree value).
        document_titles: Mapping of source filename to its document title.
            Title is required: ``main()`` enforces that every document config
            entry carries a ``title``, so this map holds an entry for every
            file in ``file_paths``. A missing entry is a programming error
            (not the intended path) and raises ``KeyError``.
        document_table: Name of the document table. Defaults to "document".
        content_table: Name of the content table. Defaults to "document_content".

    Returns:
        A tuple of (ok_files, failures). ``ok_files`` is the subset of the input
        files that were loaded or already ingested (skipped). ``failures`` is a
        list of dicts (one per file that failed), each with keys ``file``,
        ``stage`` ("load"), and ``reason``.

    Raises:
        ValueError: If an identifier (``db_schema``/``document_table``/
            ``content_table``) is invalid — a config error that aborts the step
            before any file is processed.
    """
    logger.info(f"Step 3 (load): Loading {len(file_paths)} files into {db_schema}")
    # Guard against SQL injection -- only allows [a-z0-9_] identifiers
    validate_sql_identifier(db_schema, "db_schema")
    validate_sql_identifier(document_table, "document_table")
    validate_sql_identifier(content_table, "content_table")
    cleaned_dir_path = Path(cleaned_dir)

    loaded_ok: list[str] = []
    failures: list[dict[str, str]] = []
    for file_name in file_paths:
        # Each document is loaded independently. A per-file DATA failure (missing,
        # unreadable, or malformed cleaned JSON, or a database error) is recorded
        # and the document dropped; because each document is its own transaction
        # (engine.begin per doc), a failed doc rolls back cleanly and the loop
        # proceeds. A missing collection_paths/document_titles entry is NOT caught
        # here: main() guarantees an entry per file, so a KeyError is a programming
        # bug, and (like any other unexpected exception) it propagates to main()'s
        # pipeline handler rather than being masked as a per-file data failure.
        try:
            json_path = cleaned_dir_path / (Path(file_name).stem + ".json")
            if not json_path.exists():
                raise FileNotFoundError(
                    f"Cleaned JSON not found in {cleaned_dir}: {json_path.name}"
                )

            collection_path = collection_paths[file_name]
            document_title = document_titles[file_name]
            logger.info(f"Loading: {file_name} -> {collection_path} (from {json_path})")

            # Validate and parse the cleaned JSON against the shared contract. A
            # malformed file is rejected here, before any transaction is opened.
            cleaned = CleanedDocument.model_validate_json(
                json_path.read_text(encoding="utf-8")
            )

            # The whole per-document decision (existence check, skip/overwrite,
            # delete, inserts) runs in a SINGLE transaction so the read-then-write
            # cannot straddle two connections and act on stale state. On overwrite,
            # the delete shares this transaction so a failed reinsert rolls the
            # delete back (content rows cascade-delete with the document row). The
            # non-overwrite skip continues, committing an empty transaction.
            with engine.begin() as conn:
                # Check for an existing document by its collection_path (the PK).
                result = conn.execute(
                    text(
                        f"select collection_path from {db_schema}.{document_table} "
                        "where collection_path = :cp"
                    ),
                    {"cp": collection_path},
                )
                existing = result.fetchone()

                if existing and not overwrite:
                    logger.info(f"Skipping {collection_path}: already ingested")
                    # An already-ingested document is a survivor: record it and
                    # continue out of the with-block (the insert-path append
                    # below is skipped, so it is counted exactly once).
                    loaded_ok.append(file_name)
                    continue

                if existing and overwrite:
                    logger.info(
                        f"Overwrite enabled: deleting existing document {collection_path}"
                    )
                    conn.execute(
                        text(
                            f"delete from {db_schema}.{document_table} "
                            "where collection_path = :cp"
                        ),
                        {"cp": collection_path},
                    )

                conn.execute(
                    text(
                        f"insert into {db_schema}.{document_table} "
                        "(collection_path, title, n_parsed_sections, source_binary_hash) "
                        "values (:cp, :title, :n_sections, :source_binary_hash)"
                    ),
                    {
                        "cp": collection_path,
                        "title": document_title,
                        "n_sections": cleaned.document.n_parsed_sections,
                        "source_binary_hash": cleaned.document.binary_hash,
                    },
                )
                logger.info(
                    f"Inserted document: {collection_path}, title={document_title}, "
                    f"sections={cleaned.document.n_parsed_sections}, "
                    f"source_binary_hash={cleaned.document.binary_hash}"
                )

                for section in cleaned.sections:
                    conn.execute(
                        text(
                            f"insert into {db_schema}.{content_table} "
                            "(collection_path, sort_order, heading_text, content_text, "
                            "word_count, page_start, page_end) "
                            "values (:cp, :sort_order, :heading, :content, :wc, "
                            ":page_start, :page_end)"
                        ),
                        {
                            "cp": collection_path,
                            "sort_order": section.sort_order,
                            "heading": section.heading_text,
                            "content": section.content_text,
                            "wc": section.word_count,
                            "page_start": section.page_start,
                            "page_end": section.page_end,
                        },
                    )

                logger.info(
                    f"Inserted {len(cleaned.sections)} sections for {collection_path}"
                )

            loaded_ok.append(file_name)
        # OSError covers the missing cleaned JSON (FileNotFoundError) and any
        # read error (permission denied, or the file removed after the exists()
        # check); ValueError covers a UnicodeDecodeError on a corrupt file.
        # pydantic.ValidationError is listed explicitly: it is not a ValueError
        # subclass in Pydantic v2.
        except (
            OSError,
            ValueError,
            pydantic.ValidationError,
            SQLAlchemyError,
        ) as e:
            logger.warning(f"Load failed for {file_name}: {e}")
            failures.append({"file": file_name, "stage": "load", "reason": str(e)})
            continue

    logger.info(f"Step 3 (load): Complete — {len(loaded_ok)}/{len(file_paths)} files loaded")
    return loaded_ok, failures


def resolve_db_target(config: dict) -> tuple[str, str]:
    """Resolve the target ``db_name``/``db_schema`` from a config.

    The canonical location is the TOP LEVEL of the config (matching
    ``ingest_excel`` and ``generate_embeddings``). The historical nested
    ``[load]`` form is still read as a deprecated fallback — with a WARNING —
    so existing configs on other machines keep working rather than failing at
    their next run.

    Shared with ``data_val_loaded_documents`` so the validator resolves the
    target exactly as the loader does.

    Args:
        config: Parsed TOML config dict.

    Returns:
        Tuple of (db_name, db_schema); either may be ``""`` when the config
        supplies neither form (the caller decides whether that is an error).
    """
    load_cfg = config.get("load", {})
    db_name = config.get("db_name", "")
    db_schema = config.get("db_schema", "")
    if not db_name and "db_name" in load_cfg:
        logger.warning(
            "[load].db_name is deprecated; move db_name to the top level of "
            "the config"
        )
        db_name = load_cfg["db_name"]
    if not db_schema and "db_schema" in load_cfg:
        logger.warning(
            "[load].db_schema is deprecated; move db_schema to the top level "
            "of the config"
        )
        db_schema = load_cfg["db_schema"]
    return db_name, db_schema


def validate_config(config: dict) -> None:
    """Validate the ingest config's structure and value types.

    The single, named config gate this module and its output validator
    (``data_val_loaded_documents``) both call, so a config the ingester
    accepts can never be one the validator rejects. Checks the required
    ``[module]`` block (``source_dir`` string, ``documents`` list of tables
    each carrying ``file`` and ``title`` strings), that ``overwrite`` is a
    boolean (a quoted ``"false"`` is truthy and would trigger the destructive
    delete-and-reingest path), that ``max_pages_per_batch`` is a non-negative
    integer, that the optional ``[load]`` COMMENT ON overrides are strings,
    that each enabled step's directory is configured, and that an enabled
    load step has a resolvable ``db_name``/``db_schema`` (top-level, or the
    deprecated ``[load]`` fallback).

    Per-document ``collection_path`` validity is deliberately NOT checked
    here: :func:`filter_valid_documents` warns-and-skips those per document
    rather than aborting the config.

    Args:
        config: Parsed TOML config dict.

    Raises:
        ValueError: If any required field is missing or a value has the wrong
            type (config-level errors that should abort the run).
    """
    if "module" not in config or not isinstance(config["module"], dict):
        raise ValueError("Missing required config table: [module]")
    module = config["module"]
    if "source_dir" not in module or not isinstance(module["source_dir"], str):
        raise ValueError(
            f"[module].source_dir must be a string, got {module.get('source_dir')!r}"
        )
    if "documents" not in module or not isinstance(module["documents"], list):
        raise ValueError("[module].documents must be a list of document tables")
    for index, document in enumerate(module["documents"]):
        if not isinstance(document, dict):
            raise ValueError(
                f"[[module.documents]] entry {index} must be a table, got "
                f"{type(document).__name__}"
            )
        for field in ("file", "title"):
            if field not in document or not isinstance(document[field], str):
                raise ValueError(
                    f"[[module.documents]] entry {index}: {field!r} must be a "
                    f"string, got {document.get(field)!r}"
                )

    # overwrite is the one key whose wrong type changes behaviour silently
    # rather than raising: a quoted `overwrite = "false"` is a truthy string,
    # so the run would DELETE an existing document (cascading to all its
    # content rows) instead of skipping it.
    overwrite = config.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise ValueError(f"overwrite must be a boolean, got {overwrite!r}")

    parse_cfg = config.get("parse", {})
    # A negative value would silently disable batching (fail open on the OOM
    # protection), and a non-int would crash deep in the parse. bool is
    # excluded (True/False are int subclasses).
    max_pages_per_batch = parse_cfg.get(
        "max_pages_per_batch", DEFAULT_MAX_PAGES_PER_BATCH
    )
    if (
        isinstance(max_pages_per_batch, bool)
        or not isinstance(max_pages_per_batch, int)
        or max_pages_per_batch < 0
    ):
        raise ValueError(
            f"max_pages_per_batch must be a non-negative integer, "
            f"got {max_pages_per_batch!r}"
        )

    # Optional COMMENT ON overrides (schema + the two tables): the text is
    # data (bound as a parameter), but a wrong-typed value would only surface
    # as a TypeError deep inside ensure_schema.
    load_cfg = config.get("load", {})
    for comment_key in (
        "schema_comment", "document_table_comment", "content_table_comment"
    ):
        comment_value = load_cfg.get(comment_key)
        if comment_value is not None and not isinstance(comment_value, str):
            raise ValueError(
                f"[load].{comment_key} must be a string, got {comment_value!r}"
            )

    # Require each output directory whose enabled steps actually consume it.
    # An absent key defaults to "" and Path("") resolves to ".", which
    # mkdir(exist_ok=True) happily accepts, so the run would "succeed" while
    # scattering its outputs into whatever working directory the command was
    # invoked from. parsed_dir is consumed by parse (writes) and clean
    # (reads); cleaned_dir by clean (writes) and load (reads).
    run_parse = parse_cfg.get("run", True)
    clean_cfg = config.get("clean", {})
    run_clean = clean_cfg.get("run", True)
    run_load = load_cfg.get("run", True)
    if (run_parse or run_clean) and not parse_cfg.get("parsed_dir", ""):
        raise ValueError(
            "[parse].parsed_dir is required when [parse].run or [clean].run is true"
        )
    if (run_clean or run_load) and not clean_cfg.get("cleaned_dir", ""):
        raise ValueError(
            "[clean].cleaned_dir is required when [clean].run or [load].run is true"
        )

    # An enabled load step needs a database target; resolve_db_target accepts
    # the top-level form or the deprecated [load] fallback.
    if run_load:
        db_name, db_schema = resolve_db_target(config)
        if not db_name or not isinstance(db_name, str):
            raise ValueError(
                "db_name (top-level) is required when [load].run is true"
            )
        if not db_schema or not isinstance(db_schema, str):
            raise ValueError(
                "db_schema (top-level) is required when [load].run is true"
            )


def main() -> None:
    """Entry point for document-to-database ingestion script."""
    # 1. Parse arguments (the canonical --config/--env-file pair plus this
    # script's --overwrite).
    parser = build_parser(
        "Convert source files to Docling JSON and load sections into PostgreSQL"
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        default=None,
        help="Re-parse, re-clean, and re-load existing outputs (overrides TOML config)",
    )
    args = parser.parse_args()

    # 2. Setup logging (after argparse so --help doesn't create log files):
    # INFO level, named from the config stem, anchored to the instance root.
    config_path = Path(args.config)
    setup_entry_logging("ingpipe_file_ingestion", config_path)

    with run_scope():
        # 3. Resolve credentials and load the TOML config (exits 1 on a
        # missing env file, missing config, or malformed TOML).
        config = load_config(config_path, args.env_file)

        # 4. Validate the config's structure, then extract fields.
        try:
            validate_config(config)
        except ValueError as e:
            logger.error(f"Invalid config value: {e}")
            sys.exit(1)

        try:
            module = config["module"]
            source_dir = module["source_dir"]
            overwrite = config.get("overwrite", False)
            # The CLI flag overrides the TOML value (same precedence as
            # generate_embeddings); absent means "use the config".
            if args.overwrite is not None:
                overwrite = args.overwrite
            all_documents = module["documents"]
            # Validate each document's authored collection_path (the document
            # identity, an ltree) and drop any with a missing/blank/invalid
            # value. The dropped documents are excluded from every
            # per-document map below, so parse/clean/load never see them.
            documents, collection_paths = filter_valid_documents(all_documents)
            # A config in which every entry was dropped (all collection_paths
            # missing/blank/invalid) is a fail-silent shape: the steps would
            # iterate over nothing and report success. Fail fast so the bad
            # config surfaces in the exit code. An empty all_documents is a
            # separate case left as-is.
            if all_documents and not documents:
                raise ValueError(
                    "No documents have a valid collection_path; all entries were skipped"
                )
            # validate_config guarantees `file`/`title` are strings; the str()
            # coercions here just carry that guarantee into the types.
            file_paths: list[str] = [str(d["file"]) for d in documents]
            document_titles: dict[str, str] = {
                str(d["file"]): str(d["title"]) for d in documents
            }

            # Step 1: Parse config. Parse settings resolve per file:
            # document entry -> [parse] default -> hard default.
            parse_cfg = config.get("parse", {})
            run_parse = parse_cfg.get("run", True)
            parsed_dir = parse_cfg.get("parsed_dir", "")
            parse_do_ocr_default = parse_cfg.get("do_ocr", DEFAULT_DO_OCR)
            parse_pdf_backend_default = parse_cfg.get("pdf_backend", DEFAULT_PDF_BACKEND)
            do_ocr_map: dict[str, bool] = {
                str(d["file"]): bool(d.get("do_ocr", parse_do_ocr_default))
                for d in documents
            }
            pdf_backend_map: dict[str, str] = {
                str(d["file"]): str(d.get("pdf_backend", parse_pdf_backend_default))
                for d in documents
            }
            # Run-level (not per-file): bound per-parse memory by slicing large PDFs.
            max_pages_per_batch = parse_cfg.get("max_pages_per_batch", DEFAULT_MAX_PAGES_PER_BATCH)

            # Step 2: Clean config
            clean_cfg = config.get("clean", {})
            run_clean = clean_cfg.get("run", True)
            cleaned_dir = clean_cfg.get("cleaned_dir", "")

            # Step 3: Load config. db_name/db_schema live at the TOP LEVEL
            # (the deprecated [load] form still resolves with a warning).
            load_cfg = config.get("load", {})
            run_load = load_cfg.get("run", True)
            db_name, db_schema = resolve_db_target(config)
            document_table = load_cfg.get("document_table", DOCUMENT_TABLE)
            content_table = load_cfg.get("content_table", DOCUMENT_CONTENT_TABLE)
            schema_comment = load_cfg.get("schema_comment")
            document_table_comment = load_cfg.get("document_table_comment")
            content_table_comment = load_cfg.get("content_table_comment")

            # Anchor the config's relative paths to the instance root (never
            # the working directory): an installed console script runs from
            # anywhere, so `data/...` strings must mean the same place on
            # every invocation.
            source_dir = str(resolve_config_path(source_dir, config_path))
            if parsed_dir:
                parsed_dir = str(resolve_config_path(parsed_dir, config_path))
            if cleaned_dir:
                cleaned_dir = str(resolve_config_path(cleaned_dir, config_path))
        except KeyError as e:
            logger.error(f"Missing required config field: {e}")
            sys.exit(1)
        except InstanceRootNotFoundError:
            # Already logged with the config path by require_instance_root.
            sys.exit(1)
        except ValueError as e:
            # A malformed config value (per-document collection_path
            # validation is handled separately via filter_valid_documents,
            # which skips rather than aborts).
            logger.error(f"Invalid config value: {e}")
            sys.exit(1)

        logger.info(
            f"Config loaded: config={config_path.name!r}, {len(file_paths)} documents, "
            f"do_ocr={set(do_ocr_map.values())}, pdf_backend={set(pdf_backend_map.values())}, max_pages_per_batch={max_pages_per_batch}, db={db_name}, schema={db_schema}, "
            f"tables={document_table}/{content_table}, "
            f"overwrite={overwrite}, "
            f"steps: parse={run_parse}, clean={run_clean}, load={run_load}"
        )

        n_input = len(file_paths)
        # Aggregate per-file failures across every stage. Surviving files are
        # threaded stage to stage: a file that fails at one stage is dropped,
        # but its siblings flow on. A skipped step passes its input list
        # through unchanged (a later stage records a per-file failure if an
        # input is actually missing). After the steps, main() reports a
        # summary and exits 1 if ANY file failed (0 if all clean).
        all_failures: list[dict[str, str]] = []
        try:
            # Step 1: Parse — Convert source files to Docling JSON
            if run_parse:
                parsed_ok, parse_failures = step_parse(
                    source_dir, file_paths, parsed_dir, overwrite,
                    do_ocr_map=do_ocr_map, pdf_backend_map=pdf_backend_map,
                    max_pages_per_batch=max_pages_per_batch,
                )
                all_failures.extend(parse_failures)
            else:
                logger.info("Step 1 (parse): Skipped")
                parsed_ok = file_paths

            # Step 2: Clean — Parse Docling JSON into cleaned sections JSON
            if run_clean:
                cleaned_ok, clean_failures = step_clean(
                    parsed_dir, cleaned_dir, parsed_ok, overwrite
                )
                all_failures.extend(clean_failures)
            else:
                logger.info("Step 2 (clean): Skipped")
                cleaned_ok = parsed_ok

            # Step 3: Load — Insert into database
            if run_load:
                engine = get_engine(db_name)
                # Extension contract: provisioning installs ltree, the engine
                # only verifies it — failing here, before any DDL, with an
                # actionable message rather than a privilege error
                # mid-transaction.
                require_extensions(engine, ["ltree"])
                ddl_path = Path(__file__).parent / "sql" / "schema.sql"
                ensure_schema(
                    engine, db_schema, ddl_path, document_table, content_table,
                    schema_comment=schema_comment,
                    document_table_comment=document_table_comment,
                    content_table_comment=content_table_comment,
                )
                loaded_ok, load_failures = step_load(
                    cleaned_dir, cleaned_ok, engine, db_schema, overwrite,
                    collection_paths=collection_paths, document_titles=document_titles,
                    document_table=document_table, content_table=content_table,
                )
                all_failures.extend(load_failures)
            else:
                logger.info("Step 3 (load): Skipped")
                loaded_ok = cleaned_ok

            # Pipeline summary, then the shared failure tail: each failure at
            # ERROR with a counted summary, exiting 1 if any file failed.
            parsed_n = len(parsed_ok) if run_parse else n_input
            cleaned_n = len(cleaned_ok) if run_clean else parsed_n
            loaded_n = len(loaded_ok) if run_load else cleaned_n
            logger.info(
                f"Pipeline summary: parsed {parsed_n}/{n_input}, "
                f"cleaned {cleaned_n}/{parsed_n}, loaded {loaded_n}/{cleaned_n}; "
                f"{len(all_failures)} failure(s)"
            )
            # The consolidated, post-threading failure list is the
            # authoritative ERROR report; the per-step logs emit the same
            # failures at WARNING for real-time detail, so each failure is
            # reported once at ERROR here rather than duplicated.
            finish_run(
                [
                    f"{failure['stage']}: {failure['file']} — {failure['reason']}"
                    for failure in all_failures
                ],
                success_message=f"SUCCESS: Pipeline complete for {n_input} files",
                failure_prefix=f"FAILURE: {loaded_n}/{n_input} completed",
            )
        except (FileNotFoundError, OSError, ValueError, RuntimeError, SQLAlchemyError) as e:
            logger.error(f"Pipeline failed: {e}")
            sys.exit(1)
        except Exception as e:
            # An unexpected exception here is a genuine bug (the per-step
            # handlers deliberately route programming errors to this handler),
            # so attach the traceback: the type and message alone rarely
            # locate the offending frame.
            logger.error(f"Pipeline failed with unexpected error: {e}", exc_info=True)
            sys.exit(1)


if __name__ == "__main__":
    main()
