"""Generic parse/clean quality report for the ingpipe_file_ingestion corpus.

Assesses how well documents PARSED and CLEANED across a corpus by reconciling
each parsed Docling JSON against its cleaned-sections JSON and computing generic
quality signals. This is a REPORT, not a gate: it never fails the build (exit 0
except on its own usage/config error) and it emits per-document metrics plus a
ranked "flagged for review" list.

The checks are deliberately GENERIC — they key only on Docling labels/structure
and corpus-relative statistics, never on document-specific content (e.g. no CMS
section-number logic by default). The cleaner's label constants
(``FURNITURE_LABELS``, ``HEADING_LABELS``, ``DROP_LABELS``) are reused from
``docling_section_parser`` so "what is kept/dropped" cannot drift from the
cleaner.

Checks implemented:
  1. Text coverage (parse -> clean, excluding tables): parsed non-table text
     tokens absent from the cleaned token set.
  2. Table-cell coverage (per table): table cell tokens absent from the cleaned
     content token set (content survival, not placement).
  3. Element-type audit: corpus census of element types/labels, flagging any
     UNHANDLED type (and whether it carries text).
  4. Page coverage: covered/total page ratio, with uncovered pages tagged by
     parser classification (has-body / zero-elements / blank-table /
     image-only / furniture-only). An uncovered has-body page (real content) is
     the true content-loss signal; blank-table/image-only/furniture-only are
     benign content-less pages. Non-paginated sources (e.g. Word/DOCX) report
     coverage as n/a rather than a false 0.0.
  5. Fragmentation extremes: corpus-relative Tukey-fence outliers on
     sections-per-page and median words-per-section.
  6. Text-health (prose only): replacement/control chars, single-char-word
     ratio, and broken-hyphenation counts after stripping markdown-table lines.

An optional ``--section-number-check`` flag (default off) adds a CMS-ish check
for gaps in leading heading section numbers; it never runs by default.

A document missing either JSON is reported as a per-document error entry, not a
crash. Output is a machine-readable JSON report under
``logs/ingpipe_file_ingestion/quality_report/<config_stem>.json`` plus a console
summary.

Usage:
    uv run quality-report \
        --config instances/<instance>/config/ingpipe_file_ingestion/<name>.toml
"""

import itertools
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

from docling_core.types.doc import DoclingDocument
from docling_core.types.doc.document import (
    PictureItem,
    TableItem,
    TextItem,
)
from ingpipe_lib.cli import (
    build_parser,
    load_config,
    run_scope,
    setup_entry_logging,
)
from ingpipe_lib.logconfig import get_logger
from ingpipe_lib.paths import InstanceRootNotFoundError, resolve_config_path, resolve_log_dir

# Importing the label constants from docling_section_parser keeps "what's
# kept/dropped" in lockstep with the cleaner — they cannot drift apart.
from ingpipe_file_ingestion.docling_section_parser import (
    DROP_LABELS,
    FURNITURE_LABELS,
    HEADING_LABELS,
)

logger = get_logger(__name__)

# Thresholds for Check 6 (text-health). Chosen to surface clear corruption
# without flagging ordinary prose: any replacement/control char is suspicious;
# a high single-ALPHABETIC-char-word ratio signals OCR letter-spacing (the
# garbling signature is spaced-out LETTERS like "S e l f", not numeric content
# like "1 2 3" or dollar figures, so only single letters count toward the
# ratio); and broken-hyphenation only flags when it is well above the benign
# mass of line-break hyphenation that ordinary PDF prose carries. The
# hyphenation threshold was tuned against the corpus distribution (the benign
# tail runs to ~10 per section; genuine garbling sits far above it).
SINGLE_CHAR_WORD_RATIO_THRESHOLD = 0.15
BROKEN_HYPHENATION_THRESHOLD = 10

# Tokenization regexes, compiled once at module scope: a decimal number
# ("42.1") is one token, everything else splits on word characters. (The
# broken-hyphenation heuristic — a hyphen-final token joined to a
# lowercase-initial next token, e.g. "benefi-" "ciary" — lives in
# _section_text_health, not here.)
_DECIMAL_RE = re.compile(r"\d+\.\d+")
_TOKEN_RE = re.compile(r"\d+\.\d+|\w+", re.UNICODE)
# A markdown table row is pipe-delimited; a separator row is dashes/pipes/colons
# only. Either is stripped before computing prose text-health.
_MD_TABLE_SEP_RE = re.compile(r"^[\s|:-]+$")


def _normalize_tokens(text: str) -> set[str]:
    """Normalize text to a set of comparable word tokens.

    Lowercases, extracts alphanumeric word tokens (keeping decimal numbers as
    single tokens), and number-normalizes decimals by stripping trailing zeros
    after the decimal point and a bare trailing point (``89.50`` -> ``89.5``,
    ``92.00`` -> ``92``). Integers are never altered (so ``100`` and ``2020``
    survive intact). Shared by Checks 1 and 2 so number formatting cannot
    false-flag a coverage gap.

    Args:
        text: Arbitrary text to normalize.

    Returns:
        The set of normalized tokens (empty for empty/None-like input).
    """
    tokens: set[str] = set()
    for raw in _TOKEN_RE.findall(text.lower()):
        if _DECIMAL_RE.fullmatch(raw):
            # Only decimals are reformatted; rstrip on an integer would corrupt
            # it ("100" -> "1"), so the fullmatch guard is load-bearing.
            raw = raw.rstrip("0").rstrip(".")
        tokens.add(raw)
    return tokens


def _page_nos(item: Any) -> list[int]:
    """Extract provenance page numbers from a Docling item.

    Args:
        item: A Docling document item.

    Returns:
        The 1-based page numbers from the item's provenance, or an empty list
        when the item carries no provenance.
    """
    prov = getattr(item, "prov", None)
    if not prov:
        return []
    return [p.page_no for p in prov]


def _cleaned_content_tokens(cleaned: dict[str, Any]) -> set[str]:
    """Collect normalized tokens from every section's content_text.

    Args:
        cleaned: A parsed cleaned-document dict (the ``CleanedDocument`` shape).

    Returns:
        The union of normalized tokens across all ``content_text`` values (used
        by Check 2, where table text renders into content).
    """
    tokens: set[str] = set()
    for section in cleaned.get("sections", []):
        content = section.get("content_text")
        if content:
            tokens |= _normalize_tokens(content)
    return tokens


def _cleaned_full_tokens(cleaned: dict[str, Any]) -> set[str]:
    """Collect normalized tokens from every heading_text and content_text.

    Args:
        cleaned: A parsed cleaned-document dict.

    Returns:
        The union of normalized tokens across all ``heading_text`` and
        ``content_text`` values (used by Check 1, a superset of content tokens).
    """
    tokens: set[str] = set()
    for section in cleaned.get("sections", []):
        for key in ("heading_text", "content_text"):
            value = section.get(key)
            if value:
                tokens |= _normalize_tokens(value)
    return tokens


def check_text_coverage(
    doc: DoclingDocument, cleaned: dict[str, Any]
) -> dict[str, Any]:
    """Check 1 — text coverage from parse to clean, excluding tables.

    Collects the normalized token set of every kept-eligible NON-TABLE text
    element in the parsed document (headings and text items with text),
    excluding furniture, standalone captions, pictures, and tables (tables are
    Check 2's responsibility). Compares against the cleaned document's full
    token set (headings + content). Tokens present in the parsed set but absent
    from the cleaned set are candidate dropped text.

    Args:
        doc: The parsed Docling document.
        cleaned: The parsed cleaned-document dict.

    Returns:
        A dict with ``parsed_token_count``, ``missing_tokens`` (sorted list of
        candidate dropped tokens), and ``missing_count``.
    """
    cleaned_tokens = _cleaned_full_tokens(cleaned)
    parsed_tokens: set[str] = set()
    for item, _level in doc.iterate_items():
        label = str(getattr(item, "label", "") or "")
        if label in FURNITURE_LABELS or label in DROP_LABELS:
            continue
        if isinstance(item, (TableItem, PictureItem)):
            continue
        if label in HEADING_LABELS or isinstance(item, TextItem):
            text = (getattr(item, "text", "") or "").strip()
            if text:
                parsed_tokens |= _normalize_tokens(text)

    missing = sorted(parsed_tokens - cleaned_tokens)
    return {
        "parsed_token_count": len(parsed_tokens),
        "missing_tokens": missing,
        "missing_count": len(missing),
    }


def check_table_coverage(
    doc: DoclingDocument, cleaned: dict[str, Any]
) -> dict[str, Any]:
    """Check 2 — table-cell content coverage, per table.

    For each ``TableItem`` in reading order: if the grid has any non-blank
    cell, the expected text is the set of non-blank cell texts; if the grid is
    all-blank, the expected text is the table's caption (mirroring the
    cleaner's blank-grid path). Each expected text's normalized tokens must all
    appear in the cleaned content token set. Uses the same normalization as
    Check 1 so number formatting never false-flags.

    Scope note: this verifies cell CONTENT survives, not cell PLACEMENT —
    merged or transposed cells can pass here yet be structurally garbled.

    Args:
        doc: The parsed Docling document.
        cleaned: The parsed cleaned-document dict.

    Returns:
        A dict with ``table_count`` and ``tables`` — a list of per-table
        entries that have missing tokens, each carrying ``reading_order_index``,
        ``page``, ``missing_tokens``, and ``missing_count``.
    """
    cleaned_tokens = _cleaned_content_tokens(cleaned)
    flagged_tables: list[dict[str, Any]] = []
    table_index = 0
    for item, _level in doc.iterate_items():
        if not isinstance(item, TableItem):
            continue
        table_index += 1
        grid = item.data.grid if item.data else []
        cell_texts = [
            cell.text.strip()
            for row in grid
            for cell in row
            if cell.text and cell.text.strip()
        ]
        if cell_texts:
            expected = cell_texts
        else:
            # All-blank grid: the cleaner keeps only the caption, so that is the
            # text we expect to survive.
            caption = item.caption_text(doc).strip()
            expected = [caption] if caption else []

        expected_tokens: set[str] = set()
        for text in expected:
            expected_tokens |= _normalize_tokens(text)
        missing = sorted(expected_tokens - cleaned_tokens)
        if missing:
            pages = _page_nos(item)
            flagged_tables.append(
                {
                    "reading_order_index": table_index,
                    "page": pages[0] if pages else None,
                    "missing_tokens": missing,
                    "missing_count": len(missing),
                }
            )
    return {"table_count": table_index, "tables": flagged_tables}


def check_element_types(doc: DoclingDocument) -> dict[str, Any]:
    """Check 3 — classify every parsed element as handled or unhandled.

    Walks ``iterate_items()`` and classifies each element by the same rules the
    cleaner uses: furniture label, drop label, ``TableItem``, ``PictureItem``,
    heading label, or ``TextItem`` are HANDLED; anything else is UNHANDLED. For
    unhandled elements it records whether the element carries text (via
    ``getattr(item, 'text', None)``) — a text-less unhandled element is a benign
    structural container (e.g. ``GroupItem``), whereas a text-bearing one is the
    real hazard (already gated at runtime by the cleaner's fail-fast guard).

    Args:
        doc: The parsed Docling document.

    Returns:
        A dict mapping the per-document census: ``census`` is a list of
        ``{type, label, count}`` entries; ``unhandled`` is a list of
        ``{type, label, count, carries_text}`` entries for unhandled types.
    """
    census: dict[tuple[str, str], int] = {}
    unhandled: dict[tuple[str, str], dict[str, Any]] = {}
    for item, _level in doc.iterate_items():
        type_name = type(item).__name__
        label = str(getattr(item, "label", "") or "")
        key = (type_name, label)
        census[key] = census.get(key, 0) + 1

        handled = (
            label in FURNITURE_LABELS
            or label in DROP_LABELS
            or label in HEADING_LABELS
            or isinstance(item, (TableItem, PictureItem, TextItem))
        )
        if not handled:
            text = getattr(item, "text", None)
            carries_text = bool(text and str(text).strip())
            entry = unhandled.setdefault(
                key, {"type": type_name, "label": label, "count": 0, "carries_text": False}
            )
            entry["count"] += 1
            entry["carries_text"] = entry["carries_text"] or carries_text

    return {
        "census": [
            {"type": t, "label": lbl, "count": n}
            for (t, lbl), n in sorted(census.items())
        ],
        "unhandled": list(unhandled.values()),
    }


def _is_content_bearing(item: Any, doc: DoclingDocument) -> bool:
    """Return True when an element carries real, keepable content.

    Mirrors the cleaner's keep rules so this report cannot drift from what the
    cleaner actually retains:

    - a heading / ``TextItem`` with non-empty stripped text,
    - a ``TableItem`` with >=1 non-blank grid cell OR a non-empty caption
      (the cleaner keeps a blank-grid table when it carries a caption), or
    - a ``PictureItem`` with a non-empty caption.

    Furniture and standalone captions (drop labels) are never content-bearing.

    Args:
        item: A Docling document item.
        doc: The owning Docling document (needed to resolve captions).

    Returns:
        True when the element would contribute kept content to a section.
    """
    label = str(getattr(item, "label", "") or "")
    if label in FURNITURE_LABELS or label in DROP_LABELS:
        return False
    if isinstance(item, TableItem):
        grid = item.data.grid if item.data else []
        has_cell = any(
            cell.text and cell.text.strip() for row in grid for cell in row
        )
        if has_cell:
            return True
        return bool(item.caption_text(doc).strip())
    if isinstance(item, PictureItem):
        return bool(item.caption_text(doc).strip())
    if label in HEADING_LABELS or isinstance(item, TextItem):
        return bool((getattr(item, "text", "") or "").strip())
    return False


def _classify_pages(doc: DoclingDocument) -> tuple[int, dict[int, str]]:
    """Classify each parser page by what content the parser found on it.

    A page is ``has-body`` ONLY when it carries at least one content-bearing
    element (see :func:`_is_content_bearing` — real text, a non-blank/captioned
    table, or a captioned picture). Otherwise it is reclassified by what it does
    carry: ``blank-table`` (only blank, caption-less table(s) — a benign
    Docling form/graphical mis-detection the cleaner correctly drops),
    ``image-only`` (only caption-less picture(s)), ``furniture-only`` (only
    furniture or other content-less elements), or ``zero-elements`` (no
    provenance-bearing element attributed to the page at all).

    The invariant is ``has-body ⟺ real content``: only ``has-body`` and
    ``zero-elements`` are content/coverage concerns when uncovered; the other
    tags are benign content-less pages.

    Args:
        doc: The parsed Docling document.

    Returns:
        A tuple ``(total_pages, classification)`` where total_pages is
        ``len(doc.pages)`` (falling back to the max prov.page_no, so 0 when the
        source is non-paginated), and classification maps page_no -> tag for
        every page 1..total_pages.
    """
    has_content: dict[int, bool] = {}
    has_blank_table: dict[int, bool] = {}
    has_picture: dict[int, bool] = {}
    has_any: dict[int, bool] = {}
    max_page = 0
    for item, _level in doc.iterate_items():
        is_content = _is_content_bearing(item, doc)
        is_picture = isinstance(item, PictureItem)
        is_blank_table = isinstance(item, TableItem) and not is_content
        for page in _page_nos(item):
            max_page = max(max_page, page)
            has_any[page] = True
            if is_content:
                has_content[page] = True
            if is_blank_table:
                has_blank_table[page] = True
            if is_picture and not is_content:
                has_picture[page] = True

    # max(doc.pages), not len(doc.pages): page keys need not be contiguous
    # (batched parses stitch page-range slices back together), so the count
    # would understate the extent whenever a key is missing.
    total_pages = max(doc.pages) if doc.pages else max_page
    classification: dict[int, str] = {}
    for page in range(1, total_pages + 1):
        if has_content.get(page):
            classification[page] = "has-body"
        elif has_blank_table.get(page):
            classification[page] = "blank-table"
        elif has_picture.get(page):
            classification[page] = "image-only"
        elif has_any.get(page):
            # Provenance-bearing but content-less (e.g. only furniture or
            # standalone captions). Never falls through to has-body — the
            # has-body ⟺ real-content invariant must hold.
            classification[page] = "furniture-only"
        else:
            classification[page] = "zero-elements"
    return total_pages, classification


def check_page_coverage(
    doc: DoclingDocument, cleaned: dict[str, Any]
) -> dict[str, Any]:
    """Check 4 — page coverage between parser pages and cleaned sections.

    Computes total parser pages and the parser classification of each page,
    then the set of pages covered by the cleaned sections (the union of every
    section's ``page_start..page_end``). Coverage is covered/total. Each
    uncovered page is tagged with its parser classification (see
    :func:`_classify_pages`):

    - ``has-body`` — uncovered page with REAL content; this is the true
      content-loss signal,
    - ``zero-elements`` — a strong suspect (likely un-OCR'd/scanned),
    - ``blank-table`` — benign, content-less (a blank-table Docling
      mis-detection the cleaner correctly drops),
    - ``image-only`` — a known caption-less-image limit (benign),
    - ``furniture-only`` — benign.

    Non-paginated sources (e.g. Word/DOCX) carry no usable page model, so
    ``total_pages`` degenerates to 0. In that case coverage is NOT APPLICABLE:
    ``coverage`` is None, the document is tagged ``non-paginated``, and neither
    coverage flag fires (a 0.0 coverage and a low-coverage flag would both be
    false signals).

    Args:
        doc: The parsed Docling document.
        cleaned: The parsed cleaned-document dict.

    Returns:
        A dict with ``total_pages``, ``covered_pages``, ``coverage`` (ratio, or
        None for a non-paginated source), ``classification`` (overall page-model
        tag), ``classification_breakdown`` (count of uncovered pages by tag),
        ``uncovered`` (list of ``{page, classification}``),
        ``has_uncovered_zero_element`` (bool), and ``has_uncovered_has_body``
        (bool — the true content-loss signal).
    """
    total_pages, classification = _classify_pages(doc)

    # Non-paginated source (no page model and no prov page_no): coverage is not
    # applicable. Return early so we never emit a false 0.0 / low-coverage flag.
    if total_pages == 0:
        return {
            "total_pages": 0,
            "covered_pages": 0,
            "coverage": None,
            "classification": "non-paginated",
            "classification_breakdown": {},
            "uncovered": [],
            "has_uncovered_zero_element": False,
            "has_uncovered_has_body": False,
        }

    covered: set[int] = set()
    for section in cleaned.get("sections", []):
        start = section.get("page_start")
        end = section.get("page_end")
        if start is not None and end is not None:
            covered.update(range(start, end + 1))

    all_pages = set(range(1, total_pages + 1))
    uncovered = sorted(all_pages - covered)
    uncovered_entries: list[dict[str, Any]] = [
        {"page": p, "classification": classification.get(p, "zero-elements")}
        for p in uncovered
    ]
    breakdown: dict[str, int] = {}
    for entry in uncovered_entries:
        tag = str(entry["classification"])
        breakdown[tag] = breakdown.get(tag, 0) + 1

    has_zero = breakdown.get("zero-elements", 0) > 0
    has_body_uncovered = breakdown.get("has-body", 0) > 0
    covered_pages = len(covered & all_pages)
    coverage = covered_pages / total_pages
    return {
        "total_pages": total_pages,
        "covered_pages": covered_pages,
        "coverage": round(coverage, 4),
        "classification": "paginated",
        "classification_breakdown": breakdown,
        "uncovered": uncovered_entries,
        "has_uncovered_zero_element": has_zero,
        "has_uncovered_has_body": has_body_uncovered,
    }


def _strip_markdown_tables(text: str) -> str:
    """Remove markdown-table lines from text, leaving prose.

    Drops lines that are pipe-delimited table rows or dash/pipe/colon separator
    rows so table syntax does not pollute the prose text-health metrics.

    Args:
        text: Section content text (may contain inline markdown tables).

    Returns:
        The text with table-row lines removed.
    """
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            kept.append(line)
            continue
        if stripped.startswith("|") or _MD_TABLE_SEP_RE.fullmatch(stripped):
            continue
        kept.append(line)
    return "\n".join(kept)


def _section_text_health(prose: str) -> dict[str, Any]:
    """Compute prose text-health metrics for one section's prose.

    Args:
        prose: Section prose with markdown tables already stripped.

    Returns:
        A dict with ``replacement_control_chars``,
        ``single_char_word_ratio`` (single-ALPHABETIC-char tokens / all tokens),
        ``broken_hyphenations``, and ``token_count``.
    """
    # Replacement char (U+FFFD) plus control chars, but NOT ordinary whitespace
    # (\t \n \r are category Cc and would otherwise flag every multi-line text).
    bad_chars = sum(
        1
        for ch in prose
        if ch == "�"
        or (unicodedata.category(ch) == "Cc" and ch not in "\t\n\r")
    )

    # Only single ALPHABETIC tokens count toward the ratio: the garbling
    # signature is spaced-out letters ("S e l f"), whereas single digits and
    # single punctuation come from numeric/code tables (revision codes, dollar
    # figures) and are benign — counting them produced the bulk of the false
    # flags this metric used to emit. The denominator stays ALL tokens.
    tokens = prose.split()
    single_char = sum(1 for t in tokens if len(t) == 1 and t.isalpha())
    ratio = (single_char / len(tokens)) if tokens else 0.0

    broken = 0
    for current, nxt in itertools.pairwise(tokens):
        if current.endswith("-") and nxt[:1].islower():
            broken += 1

    return {
        "replacement_control_chars": bad_chars,
        "single_char_word_ratio": round(ratio, 4),
        "broken_hyphenations": broken,
        "token_count": len(tokens),
    }


def check_text_health(cleaned: dict[str, Any]) -> dict[str, Any]:
    """Check 6 — prose text-health across the cleaned sections.

    For each section's ``content_text``, strips markdown-table lines first,
    then computes on the remaining prose: replacement/control-char count,
    single-ALPHABETIC-char-word ratio (single digits/punctuation are excluded so
    numeric tables do not false-flag), and broken-hyphenation count. A section
    is flagged when it has any replacement/control char, a single-char-word
    ratio above ``SINGLE_CHAR_WORD_RATIO_THRESHOLD``, or broken hyphenations
    above ``BROKEN_HYPHENATION_THRESHOLD``.

    Args:
        cleaned: The parsed cleaned-document dict.

    Returns:
        A dict with ``flagged_sections`` (sorted worst-first by a severity
        score) and ``flagged_count``.
    """
    flagged: list[dict[str, Any]] = []
    for section in cleaned.get("sections", []):
        content = section.get("content_text")
        if not content:
            continue
        prose = _strip_markdown_tables(content)
        metrics = _section_text_health(prose)
        is_flagged = (
            metrics["replacement_control_chars"] > 0
            or metrics["single_char_word_ratio"] > SINGLE_CHAR_WORD_RATIO_THRESHOLD
            or metrics["broken_hyphenations"] > BROKEN_HYPHENATION_THRESHOLD
        )
        if is_flagged:
            # Severity score orders the worst sections first for reporting.
            severity = (
                metrics["replacement_control_chars"] * 100
                + metrics["single_char_word_ratio"] * 10
                + metrics["broken_hyphenations"]
            )
            flagged.append(
                {
                    "sort_order": section.get("sort_order"),
                    "heading_text": section.get("heading_text"),
                    **metrics,
                    "severity": round(severity, 4),
                }
            )
    flagged.sort(key=lambda s: s["severity"], reverse=True)
    return {"flagged_sections": flagged, "flagged_count": len(flagged)}


def compute_fragmentation_metrics(
    cleaned: dict[str, Any], total_pages: int
) -> dict[str, Any]:
    """Compute per-document fragmentation metrics (first pass of Check 5).

    Args:
        cleaned: The parsed cleaned-document dict.
        total_pages: The document's total parser page count.

    Returns:
        A dict with ``section_count``, ``total_pages``, ``sections_per_page``,
        and ``median_words_per_section``.
    """
    sections = cleaned.get("sections", [])
    section_count = len(sections)
    pages = total_pages if total_pages > 0 else 1
    sections_per_page = section_count / pages

    word_counts = sorted(s.get("word_count", 0) for s in sections)
    median_words = _median(word_counts)
    return {
        "section_count": section_count,
        "total_pages": total_pages,
        "sections_per_page": round(sections_per_page, 4),
        "median_words_per_section": median_words,
    }


def _median(values: list[float]) -> float:
    """Return the median of a list of numbers (0.0 when empty).

    Args:
        values: A list of numbers (need not be sorted).

    Returns:
        The median, or 0.0 for an empty list.
    """
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return float(s[mid])
    return (s[mid - 1] + s[mid]) / 2


def _tukey_fences(values: list[float]) -> tuple[float, float]:
    """Compute Tukey outlier fences (Q1 - 1.5*IQR, Q3 + 1.5*IQR).

    Args:
        values: The distribution to fence (>= 1 value).

    Returns:
        A tuple ``(lower_fence, upper_fence)``. With too few distinct points the
        fences collapse toward the data, which is acceptable for a report.
    """
    s = sorted(values)
    n = len(s)
    # Linear-interpolation quartiles on the sorted sample.
    def _quantile(q: float) -> float:
        if n == 1:
            return s[0]
        pos = q * (n - 1)
        lo = int(pos)
        frac = pos - lo
        if lo + 1 < n:
            return s[lo] + frac * (s[lo + 1] - s[lo])
        return s[lo]

    q1 = _quantile(0.25)
    q3 = _quantile(0.75)
    iqr = q3 - q1
    return q1 - 1.5 * iqr, q3 + 1.5 * iqr


def flag_fragmentation(
    per_doc_metrics: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Flag fragmentation extremes corpus-relative (second pass of Check 5).

    Applies Tukey fences to the corpus distributions of ``sections_per_page``
    and ``median_words_per_section``. A document with high sections-per-page and
    low words-per-section is over-fragmented; low + high is under-segmented. A
    document is flagged only when it is an outlier on at least one metric in the
    over-fragmentation or under-segmentation direction.

    Args:
        per_doc_metrics: Mapping of doc stem -> the fragmentation metrics dict
            from :func:`compute_fragmentation_metrics`.

    Returns:
        Mapping of doc stem -> a flag dict ``{flag, reasons}`` for every
        flagged document (only flagged docs are included).
    """
    if not per_doc_metrics:
        return {}
    # sections_per_page is undefined for a non-paginated source (no page count),
    # so non-paginated docs are excluded from both the fence and the flag —
    # mirroring how _coverage_threshold excludes coverage-None docs — otherwise a
    # large Word/DOCX doc would present an inflated sections_per_page and become a
    # false over-fragmentation outlier in a mixed corpus. median_words_per_section
    # is page-independent, so every doc stays in that distribution.
    spp = [
        m["sections_per_page"]
        for m in per_doc_metrics.values()
        if m["total_pages"] > 0
    ]
    mws = [m["median_words_per_section"] for m in per_doc_metrics.values()]
    spp_lo, spp_hi = _tukey_fences(spp) if spp else (0.0, 0.0)
    mws_lo, mws_hi = _tukey_fences(mws)

    flags: dict[str, dict[str, Any]] = {}
    for stem, m in per_doc_metrics.items():
        reasons: list[str] = []
        paginated = m["total_pages"] > 0
        # sections_per_page legs only apply to paginated docs.
        high_spp = paginated and m["sections_per_page"] > spp_hi
        low_spp = paginated and m["sections_per_page"] < spp_lo
        high_mws = m["median_words_per_section"] > mws_hi
        low_mws = m["median_words_per_section"] < mws_lo
        if high_spp:
            reasons.append(
                f"over-fragmentation: sections_per_page "
                f"{m['sections_per_page']} > fence {round(spp_hi, 2)}"
            )
        if low_mws:
            reasons.append(
                f"over-fragmentation: median_words_per_section "
                f"{m['median_words_per_section']} < fence {round(mws_lo, 2)}"
            )
        if low_spp:
            reasons.append(
                f"under-segmentation: sections_per_page "
                f"{m['sections_per_page']} < fence {round(spp_lo, 2)}"
            )
        if high_mws:
            reasons.append(
                f"under-segmentation: median_words_per_section "
                f"{m['median_words_per_section']} > fence {round(mws_hi, 2)}"
            )
        if reasons:
            flags[stem] = {"flag": True, "reasons": reasons}
    return flags


def check_section_numbers(doc: DoclingDocument) -> dict[str, Any]:
    """Optional CMS-ish check: detect gaps in leading heading section numbers.

    OFF by default; only invoked when ``--section-number-check`` is set. Detects
    a leading dotted/plain section number at the start of each heading (e.g.
    ``10.2`` or ``20 -``) and reports gaps in the top-level sequence. This is a
    naive heuristic: CMS sections commonly step by 10 (10, 20, 30, ...), so the
    expected step is inferred as the smallest positive gap between consecutive
    top-level numbers, and only jumps LARGER than that step are reported (with
    the expected-but-missing values). This is deliberately content-specific and
    never runs in the default report.

    Args:
        doc: The parsed Docling document.

    Returns:
        A dict with ``top_level_numbers`` (sorted unique leading integers),
        ``inferred_step`` (the smallest positive consecutive gap, or None), and
        ``gaps`` (the expected-but-missing values implied by jumps larger than
        the inferred step).
    """
    leading_re = re.compile(r"^\s*(\d+)(?:\.\d+)*\b")
    top_levels: set[int] = set()
    for item, _level in doc.iterate_items():
        label = str(getattr(item, "label", "") or "")
        if label not in HEADING_LABELS:
            continue
        text = (getattr(item, "text", "") or "").strip()
        match = leading_re.match(text)
        if match:
            top_levels.add(int(match.group(1)))

    numbers = sorted(top_levels)
    # Infer the expected step from the data (the smallest positive consecutive
    # gap) so a CMS step-of-10 sequence is not reported as ~9 gaps per interval.
    diffs = [b - a for a, b in itertools.pairwise(numbers) if b - a > 0]
    step = min(diffs) if diffs else None

    gaps: list[int] = []
    if step:
        for a, b in itertools.pairwise(numbers):
            jump = b - a
            # A jump larger than the inferred step suggests missing entries; the
            # missing values are the step-spaced points between a and b.
            if jump > step:
                gaps.extend(range(a + step, b, step))
    return {"top_level_numbers": numbers, "inferred_step": step, "gaps": gaps}


def analyze_document(
    stem: str,
    parsed_dir: Path,
    cleaned_dir: Path,
    section_number_check: bool,
) -> dict[str, Any]:
    """Run all per-document checks for one document stem.

    Reads ``parsed_dir/<stem>.json`` (a Docling document) and
    ``cleaned_dir/<stem>.json`` (the cleaned-sections dict) and runs the
    document-scoped checks (1, 2, 3, 4, 6, the fragmentation first pass, and the
    optional section-number check). A missing or unreadable input is returned as
    an ``error`` entry rather than raised.

    Args:
        stem: The document file stem.
        parsed_dir: Directory holding the parsed Docling JSON.
        cleaned_dir: Directory holding the cleaned-sections JSON.
        section_number_check: Whether to run the optional section-number check.

    Returns:
        A per-document result dict. On failure it carries ``{"stem", "error"}``;
        on success it carries the per-check results plus ``fragmentation``.
    """
    parsed_path = parsed_dir / f"{stem}.json"
    cleaned_path = cleaned_dir / f"{stem}.json"

    if not parsed_path.exists():
        return {"stem": stem, "error": f"parsed JSON not found: {parsed_path}"}
    if not cleaned_path.exists():
        return {"stem": stem, "error": f"cleaned JSON not found: {cleaned_path}"}

    try:
        doc = DoclingDocument.load_from_json(parsed_path)
    except (OSError, ValueError) as e:
        # load_from_json raises pydantic ValidationError (a ValueError subclass)
        # on malformed/invalid-schema JSON, or OSError on a read failure — both
        # are isolated as a per-document error, consistent with the cleaned-JSON
        # catch below.
        return {"stem": stem, "error": f"could not load parsed JSON: {e}"}

    try:
        cleaned = json.loads(cleaned_path.read_bytes())
    except (OSError, ValueError) as e:
        return {"stem": stem, "error": f"could not read cleaned JSON: {e}"}

    result: dict[str, Any] = {"stem": stem}
    try:
        result["text_coverage"] = check_text_coverage(doc, cleaned)
        result["table_coverage"] = check_table_coverage(doc, cleaned)
        result["element_types"] = check_element_types(doc)
        page_coverage = check_page_coverage(doc, cleaned)
        result["page_coverage"] = page_coverage
        result["text_health"] = check_text_health(cleaned)
        result["fragmentation"] = compute_fragmentation_metrics(
            cleaned, page_coverage["total_pages"]
        )
        if section_number_check:
            result["section_numbers"] = check_section_numbers(doc)
    except Exception as e:  # noqa: BLE001 — deliberate isolation boundary (see below)
        # Deliberate per-document isolation boundary: a malformed cleaned dict
        # can raise a wide range of types (KeyError/TypeError/AttributeError/...)
        # from any of the checks, and this report must never crash or change the
        # exit code on a single bad document. The broad catch converts any such
        # failure into a per-document error entry — the report-not-gate contract.
        return {"stem": stem, "error": f"check failed: {e}"}

    return result


def _collect_flags(
    result: dict[str, Any],
    coverage_threshold: float,
    fragmentation_flag: dict[str, Any] | None,
) -> list[str]:
    """Assemble the human-readable flag reasons for one document.

    Args:
        result: A successful per-document result dict.
        coverage_threshold: Corpus-relative low-coverage fence; a document below
            this OR with any uncovered zero-element page is flagged.
        fragmentation_flag: The fragmentation flag dict for this document, or
            None when not flagged.

    Returns:
        A list of brief, check-tagged reasons (empty when nothing flagged).
    """
    reasons: list[str] = []

    tc = result["text_coverage"]
    if tc["missing_count"] > 0:
        sample = ", ".join(tc["missing_tokens"][:5])
        reasons.append(
            f"check1 text-coverage: {tc['missing_count']} parsed token(s) "
            f"absent from cleaned (e.g. {sample})"
        )

    tbl = result["table_coverage"]
    if tbl["tables"]:
        reasons.append(
            f"check2 table-coverage: {len(tbl['tables'])} table(s) with "
            f"missing cell tokens"
        )

    unhandled = result["element_types"]["unhandled"]
    text_bearing = [u for u in unhandled if u["carries_text"]]
    if text_bearing:
        types = ", ".join(sorted({u["type"] for u in text_bearing}))
        reasons.append(
            f"check3 element-audit: text-bearing unhandled element type(s): {types}"
        )

    pc = result["page_coverage"]
    # coverage is None for non-paginated sources; n/a never flags on coverage.
    if pc["coverage"] is not None and pc["coverage"] < coverage_threshold:
        reasons.append(
            f"check4 page-coverage: coverage {pc['coverage']:.4f} below "
            f"corpus fence {coverage_threshold:.4f}"
        )
    if pc.get("has_uncovered_has_body"):
        body_pages = [
            e["page"] for e in pc["uncovered"] if e["classification"] == "has-body"
        ]
        reasons.append(
            f"check4 page-coverage: uncovered has-body page(s) "
            f"{body_pages} (real content not in any cleaned section — "
            f"true content loss)"
        )
    if pc["has_uncovered_zero_element"]:
        zero_pages = [
            e["page"] for e in pc["uncovered"] if e["classification"] == "zero-elements"
        ]
        reasons.append(
            f"check4 page-coverage: uncovered zero-element page(s) "
            f"{zero_pages} (likely un-OCR'd/scanned)"
        )

    if fragmentation_flag:
        for r in fragmentation_flag["reasons"]:
            reasons.append(f"check5 fragmentation: {r}")

    th = result["text_health"]
    if th["flagged_count"] > 0:
        worst = th["flagged_sections"][0]
        reasons.append(
            f"check6 text-health: {th['flagged_count']} section(s) flagged "
            f"(worst: {worst['replacement_control_chars']} bad-char, "
            f"single-char-ratio {worst['single_char_word_ratio']}, "
            f"{worst['broken_hyphenations']} broken-hyphen)"
        )

    if "section_numbers" in result and result["section_numbers"]["gaps"]:
        reasons.append(
            f"section-number-check: gaps {result['section_numbers']['gaps']}"
        )

    return reasons


def _build_census(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate the corpus-wide element-type census across documents.

    Args:
        results: All successful per-document result dicts.

    Returns:
        A sorted list of ``{type, label, count, n_docs, unhandled,
        carries_text}`` entries.
    """
    counts: dict[tuple[str, str], int] = {}
    docs: dict[tuple[str, str], set[str]] = {}
    unhandled_keys: set[tuple[str, str]] = set()
    text_bearing_keys: set[tuple[str, str]] = set()

    for result in results:
        if "error" in result:
            continue
        stem = result["stem"]
        for entry in result["element_types"]["census"]:
            key = (entry["type"], entry["label"])
            counts[key] = counts.get(key, 0) + entry["count"]
            docs.setdefault(key, set()).add(stem)
        for u in result["element_types"]["unhandled"]:
            key = (u["type"], u["label"])
            unhandled_keys.add(key)
            if u["carries_text"]:
                text_bearing_keys.add(key)

    census = [
        {
            "type": t,
            "label": lbl,
            "count": counts[(t, lbl)],
            "n_docs": len(docs[(t, lbl)]),
            "unhandled": (t, lbl) in unhandled_keys,
            "carries_text": (t, lbl) in text_bearing_keys,
        }
        for (t, lbl) in sorted(counts)
    ]
    return census


def _coverage_threshold(results: list[dict[str, Any]]) -> float:
    """Compute the corpus-relative low-coverage fence (Tukey lower fence).

    Args:
        results: All successful per-document result dicts.

    Returns:
        The Tukey lower fence on the coverage distribution, clamped to >= 0.0.
        Defaults to 0.0 when there are no paginated documents (nothing is
        flagged on coverage alone). Non-paginated documents (coverage None) are
        excluded so they neither crash the fence nor skew it.
    """
    coverages = [
        r["page_coverage"]["coverage"]
        for r in results
        if "error" not in r and r["page_coverage"]["coverage"] is not None
    ]
    if not coverages:
        return 0.0
    lower, _upper = _tukey_fences(coverages)
    return max(0.0, round(lower, 4))


def build_report(
    results: list[dict[str, Any]],
    config_stem: str,
) -> dict[str, Any]:
    """Assemble the full corpus report from per-document results.

    Runs the two-pass fragmentation flagging (Check 5), computes the
    corpus-relative coverage fence, assembles each document's flag reasons, and
    ranks the flagged documents by flag count.

    Args:
        results: All per-document result dicts (may include error entries).
        config_stem: The config file stem (used in the report metadata).

    Returns:
        The full machine-readable report dict.
    """
    successful = [r for r in results if "error" not in r]
    errors = [r for r in results if "error" in r]

    frag_metrics = {r["stem"]: r["fragmentation"] for r in successful}
    frag_flags = flag_fragmentation(frag_metrics)
    coverage_threshold = _coverage_threshold(successful)

    flagged: list[dict[str, Any]] = []
    for result in successful:
        reasons = _collect_flags(
            result, coverage_threshold, frag_flags.get(result["stem"])
        )
        result["flags"] = reasons
        if reasons:
            flagged.append({"stem": result["stem"], "flag_count": len(reasons), "reasons": reasons})
    flagged.sort(key=lambda f: f["flag_count"], reverse=True)

    census = _build_census(successful)
    return {
        "config_stem": config_stem,
        "documents_total": len(results),
        "documents_ok": len(successful),
        "documents_error": len(errors),
        "coverage_threshold": coverage_threshold,
        "element_type_census": census,
        "documents": results,
        "errors": [{"stem": e["stem"], "error": e["error"]} for e in errors],
        "flagged_for_review": flagged,
    }


def _render_console(report: dict[str, Any]) -> None:
    """Render the human-readable console summary of the report.

    The console summary is an explicit deliverable, so each line is emitted to
    stdout (logconfig's handler writes only to the JSONL file) and also logged
    at INFO so the run is fully captured in the log.

    Args:
        report: The full report dict from :func:`build_report`.
    """

    def emit(line: str) -> None:
        """Print a summary line to stdout and mirror it to the log."""
        print(line)
        logger.info(line)

    emit("=" * 60)
    emit(
        f"QUALITY REPORT: {report['config_stem']} — "
        f"{report['documents_ok']} ok, {report['documents_error']} error(s), "
        f"{report['documents_total']} total"
    )
    emit(f"Corpus low-coverage fence: {report['coverage_threshold']}")

    emit("--- Element-type census (type / label : count [n_docs]) ---")
    for entry in report["element_type_census"]:
        marker = ""
        if entry["unhandled"]:
            marker = (
                " [UNHANDLED, text-bearing]"
                if entry["carries_text"]
                else " [unhandled, text-less]"
            )
        emit(
            f"  {entry['type']} / {entry['label']!r}: {entry['count']} "
            f"({entry['n_docs']} doc(s)){marker}"
        )

    non_paginated = [
        r["stem"]
        for r in report["documents"]
        if "error" not in r
        and r["page_coverage"]["classification"] == "non-paginated"
    ]
    if non_paginated:
        emit("--- Page coverage: n/a (non-paginated source) ---")
        for stem in non_paginated:
            emit(f"  {stem}: page coverage n/a (non-paginated source)")

    for err in report["errors"]:
        emit(f"ERROR doc {err['stem']}: {err['error']}")

    flagged = report["flagged_for_review"]
    emit(f"--- Flagged for review: {len(flagged)} document(s) (ranked) ---")
    for entry in flagged:
        emit(f"  {entry['stem']} ({entry['flag_count']} flag(s)):")
        for reason in entry["reasons"]:
            emit(f"      - {reason}")
    if not flagged:
        emit("  (none)")
    emit("=" * 60)


def main() -> None:
    """Entry point for the parse/clean quality report."""
    # 1. Parse arguments (--config only; the report needs no database).
    parser = build_parser(
        "Generic parse/clean quality report for the ingpipe_file_ingestion corpus",
        env_file=False,
    )
    parser.add_argument(
        "--section-number-check",
        action="store_true",
        help="Optional CMS-ish check for gaps in leading heading section numbers (off by default)",
    )
    args = parser.parse_args()

    # 2. Setup logging (after argparse so --help doesn't create log files):
    # INFO level, named from the config stem, anchored to the instance root.
    config_path = Path(args.config)
    log_dir = resolve_log_dir("ingpipe_file_ingestion/quality_report", config_path)
    setup_entry_logging("ingpipe_file_ingestion/quality_report", config_path)

    with run_scope():
        logger.info("Starting parse/clean quality report")

        # 3. Load the TOML config (exits 1 on a missing config or malformed
        # TOML).
        config = load_config(config_path)

        # 4. Resolve directories and document stems from config. Relative
        # paths anchor to the instance root, never the working directory.
        try:
            parsed_dir = resolve_config_path(config["parse"]["parsed_dir"], config_path)
            cleaned_dir = resolve_config_path(config["clean"]["cleaned_dir"], config_path)
            documents = config["module"]["documents"]
            stems = [Path(d["file"]).stem for d in documents]
        except KeyError as e:
            logger.error(f"Missing required config field: {e}")
            sys.exit(1)
        except InstanceRootNotFoundError:
            # Already logged with the config path by require_instance_root.
            sys.exit(1)

        if not stems:
            logger.error("Config lists no documents to report on")
            sys.exit(1)
        if not parsed_dir.is_dir():
            logger.error(f"Parsed directory not found: {parsed_dir}")
            sys.exit(1)
        if not cleaned_dir.is_dir():
            logger.error(f"Cleaned directory not found: {cleaned_dir}")
            sys.exit(1)

        # 5. Run per-document checks. Each document is isolated so a single
        # bad document is reported (not raised) and never changes the exit
        # code.
        logger.info(f"Reporting on {len(stems)} document(s)")
        results = [
            analyze_document(stem, parsed_dir, cleaned_dir, args.section_number_check)
            for stem in stems
        ]

        # 6. Assemble the corpus report (two-pass fragmentation + ranking).
        config_stem = config_path.stem
        report = build_report(results, config_stem)

        # 7. Write the machine-readable report beside this run's log
        # (anchored to the instance, not the working directory).
        out_dir = log_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{config_stem}.json"
        out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        logger.info(f"Report written to {out_path}")

        # 8. Console summary. This is a report, not a gate: exit 0 even with
        # findings.
        _render_console(report)


if __name__ == "__main__":
    main()
