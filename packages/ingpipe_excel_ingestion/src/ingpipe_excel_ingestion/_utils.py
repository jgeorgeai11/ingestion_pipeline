"""Shared utilities for the generic Excel ingestion module.

Provides:
  - ``get_engine``: re-exported from the canonical ``ingpipe_lib.db``
    factory (URL.create credential encoding, port guard, logged ValueError)
    so existing call sites and cross-directory imports keep working.
  - ``validate_sql_identifier`` / ``validate_collection_path``: re-exported
    from the canonical ``ingpipe_lib.validators`` (the ``fullmatch``
    implementations) rather than duplicating them here.
  - ``normalize_column_name`` / ``deduplicate_columns``: turn Excel headers
    into unquoted-safe ``col_*`` SQL identifiers.
"""

import hashlib
import re
from pathlib import Path

# Re-exported so ingpipe_excel_ingestion's call sites and tests keep importing this
# name from _utils; the canonical copy lives in ingpipe_lib.db.
from ingpipe_lib.db import get_engine
from ingpipe_lib.logconfig import get_logger

# Re-exported so ingpipe_excel_ingestion's call sites and tests keep importing these
# names from _utils; the canonical copies live in ingpipe_lib.validators.
from ingpipe_lib.validators import validate_collection_path, validate_sql_identifier

logger = get_logger(__name__)

__all__ = [
    "compute_source_hash",
    "deduplicate_columns",
    "get_engine",
    "make_collection_path",
    "normalize_column_name",
    "validate_collection_path",
    "validate_sql_identifier",
]

# Runs of any character that is not a lowercase letter or digit collapse to a
# single underscore when snake-casing a header (applied after lowercasing).
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

# PostgreSQL truncates identifiers longer than NAMEDATALEN-1 (63 bytes by
# default) SILENTLY. An over-long generated column name would be stored
# truncated on CREATE but compared at full length on the later ADD COLUMN
# reconciliation, so the column would never match and a spurious duplicate
# ALTER would fail. We therefore cap every generated column name at this length
# up front so the name we generate is exactly the name Postgres stores.
MAX_IDENTIFIER_LENGTH = 63


def normalize_column_name(header: str, index: int) -> str:
    """Normalize an Excel header to an unquoted-safe ``col_*`` SQL identifier.

    The header is lowercased, runs of non-alphanumeric characters collapse to a
    single underscore, leading/trailing underscores are stripped, and the result
    is prefixed with ``col_``. Prefixing guarantees the name is a valid,
    unquoted-safe identifier regardless of reserved words (``window`` ->
    ``col_window``) or leading digits (``2021 Q1`` -> ``col_2021_q1``). An empty
    or degenerate header (e.g. ``"###"``) falls back to ``col_<index>``. The
    result is capped at ``MAX_IDENTIFIER_LENGTH`` (Postgres's silent-truncation
    limit) so the generated name is exactly the name Postgres stores.

    Args:
        header: The original Excel column header string.
        index: 0-based position of the header in the sheet, used for the
            ``col_<index>`` fallback when the header normalizes to empty.

    Returns:
        A ``col_``-prefixed snake_case identifier, at most
        ``MAX_IDENTIFIER_LENGTH`` characters.
    """
    snake = _NON_ALNUM_RE.sub("_", header.strip().lower()).strip("_")
    if not snake:
        return f"col_{index}"
    # Cap up front, then strip any trailing underscore the cut may have left so
    # the stored identifier matches exactly what Postgres would keep.
    return f"col_{snake}"[:MAX_IDENTIFIER_LENGTH].rstrip("_")


def _suffixed(base: str, n: int) -> str:
    """Append a ``_<n>`` collision suffix, keeping within the identifier cap.

    The base is trimmed (and any trailing underscore stripped) so that
    ``base_<n>`` is at most ``MAX_IDENTIFIER_LENGTH`` characters — otherwise
    Postgres would silently truncate the suffixed name, potentially re-creating
    the very collision the suffix is meant to resolve.

    Args:
        base: The capped, normalized base column name.
        n: The collision ordinal (2, 3, ...).

    Returns:
        The suffixed identifier, at most ``MAX_IDENTIFIER_LENGTH`` characters.
    """
    suffix = f"_{n}"
    keep = MAX_IDENTIFIER_LENGTH - len(suffix)
    return f"{base[:keep].rstrip('_')}{suffix}"


def deduplicate_columns(names: list[str]) -> list[str]:
    """Resolve collisions among normalized column names with numeric suffixes.

    The first occurrence of a name is kept as-is; subsequent collisions get a
    ``_2``, ``_3``, ... suffix in order (``col_question`` -> ``col_question``,
    ``col_question_2``). Suffixed names are themselves checked against the seen
    set so a suffix can never resurrect a prior collision.

    Args:
        names: Normalized column names (output of ``normalize_column_name``).

    Returns:
        A list of unique column names, same length and order as the input.
    """
    counts: dict[str, int] = {}
    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            counts[name] = 1
            result.append(name)
            continue
        # Find the next free suffix for this base name. `name` may already be in
        # `seen` as a previously-generated candidate rather than an original, so
        # it may have no `counts` entry yet — seed it via .get() instead of `+=`
        # to avoid a KeyError. The suffix must also fit within
        # MAX_IDENTIFIER_LENGTH, so the base is trimmed to leave room for the
        # "_<n>" suffix (otherwise Postgres would truncate the suffixed name back
        # to a length that could re-collide).
        counts[name] = counts.get(name, 1) + 1
        candidate = _suffixed(name, counts[name])
        while candidate in seen:
            counts[name] += 1
            candidate = _suffixed(name, counts[name])
        seen.add(candidate)
        result.append(candidate)
        logger.debug(f"Duplicate column {name!r} renamed to {candidate!r}")
    return result


def _ltree_label(value: str) -> str:
    """Sanitize a string into a single lowercase ``ltree`` label.

    Lowercases, collapses runs of non-alphanumeric characters to a single
    underscore, and strips leading/trailing underscores. The result contains
    only ``[a-z0-9_]`` (a valid ltree label; a leading digit is permitted).

    Args:
        value: The raw string (a filename stem or sheet name).

    Returns:
        The sanitized label, possibly empty if ``value`` had no alphanumerics.
    """
    return _NON_ALNUM_RE.sub("_", value.lower()).strip("_")


def make_collection_path(
    filename: str, sheet: str, override: str | None = None, prefix: str | None = None
) -> str:
    """Resolve the ``collection_path`` ltree identity for a sheet.

    Precedence, in one sentence: **an override wins outright, and a prefix
    applies only to a derived path.**

    If ``override`` is given (a config-authored path), it is VALIDATED as a
    lowercase ltree and returned unchanged — never rewritten and never
    prefixed, because an authored path already states the full identity
    (ingpipe_file_ingestion's validate-not-sanitize rule). Otherwise the path is
    DERIVED by sanitizing the filename stem and sheet name into two ltree
    labels joined by a dot (``"2025-codes-list-aki.xlsx"`` + ``"Triggers"`` ->
    ``2025_codes_list_aki.triggers``), and ``prefix`` — when given — is
    prepended to that derivation. Deriving (sanitizing) is appropriate here
    because the workbook/sheet names are given by the data, not authored.

    The prefix exists so a set of workbooks can sit under a schema-scoped
    branch of the ltree tree without hand-authoring an override for every
    sheet: a corpus filter like ``qpp_cm.%`` matches nothing when every path is a
    bare ``<stem>.<leaf>``, which silently drops every sheet from a
    schema-scoped query rather than reporting an error.

    The result is validated whichever way it was built, so a degenerate name
    (a stem or sheet with no alphanumerics) or a malformed prefix fails fast
    rather than yielding an invalid ltree.

    Args:
        filename: The workbook filename (with extension).
        sheet: The exact sheet/tab name.
        override: A config-authored collection_path, or None to derive.
        prefix: An ltree path prepended to a DERIVED path (e.g.
            ``"qpp_cm.2026_cost_measure_codes_lists"``). Ignored when ``override``
            is given. None means no prefix.

    Returns:
        A valid lowercase ltree collection_path.

    Raises:
        ValueError: If ``override`` is not a valid ltree, if ``prefix`` is not
            a valid ltree on its own, or if the resulting derived path is
            invalid (e.g. an all-non-alphanumeric stem or sheet).
    """
    if override is not None:
        return validate_collection_path(override)

    stem = _ltree_label(Path(filename).stem)
    leaf = _ltree_label(sheet)
    derived = f"{stem}.{leaf}"

    if prefix is not None:
        # Validate the prefix on its own first, so a malformed prefix reports
        # itself rather than surfacing as an unreadable composite path.
        validate_collection_path(prefix)
        derived = f"{prefix}.{derived}"

    return validate_collection_path(derived)


def compute_source_hash(row_texts: list[str]) -> int:
    """Compute the per-sheet content fingerprint stored as ``source_binary_hash``.

    Returns the low 64 bits of ``sha256`` over the sheet's ordered ``row_text``
    values joined by newlines. Because each ``row_text`` encodes both the column
    headers and the cell values, any header or value change flips the hash — the
    signal a monthly re-pull uses to detect which sheets changed. The low-64-bit
    form matches ingpipe_file_ingestion's ``source_binary_hash`` (an unsigned 64-bit
    value in ``[0, 2^64)``).

    Args:
        row_texts: The sheet's ``row_text`` strings in sort_order.

    Returns:
        An unsigned 64-bit integer in ``[0, 2**64)``.
    """
    joined = "\n".join(row_texts)
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()
    return int(digest, 16) & 0xFFFFFFFFFFFFFFFF


