"""Target discovery for the CMS Internet-Only Manuals (IOMs).

This module is the whole of what ``cms_iom`` still needs in code, and it is
here because CMS's targets cannot be COMPUTED -- they must be FOUND. Acquiring
this source means fetching an index, matching manual links against a pattern,
parsing the index table for each manual's human title, following each manual's
page, and scraping its ``<article>`` element for chapter PDFs. That is a
traversal algorithm; a config format expressive enough to describe it would be
a scraping DSL harder to read than this file.

Everything else that ``download_cms_iom.py`` used to do -- the download, the
retry policy, the skip decision, the manifest, the run summary, the exit
code -- now belongs to the ``ingpipe_acquisition`` engine. What is left is a
``discover(config)`` generator matching the engine's documented hook:

    discover: (config) -> Iterable[Target]

Three behaviors changed in the move, each closing an audit finding:

  - **A manual with no title in the index table now FAILS.** The old code fell
    back to slugifying the manual key, producing ``pub_100_04_100_04`` -- a
    folder name no downstream ingest config references, so the chapters landed
    somewhere nothing reads. A silent degradation that orphans a manual is
    worse than a stopped run.
  - **Chapter filenames are disambiguated.** The old code keyed each
    destination on the URL's bare basename, so two chapter URLs under different
    directories that shared a basename silently overwrote one another. Names
    stay as they are today (no collisions currently exist, and the 21
    ingpipe_file_ingestion configs name the files) and a collision now pulls in the
    URL's parent path segment instead of dropping a chapter.
  - **There is no per-manual folder skip.** The engine skips per artifact
    against the prior run's manifest, so an interrupted run now completes on a
    re-run instead of skipping every manual whose folder is non-empty.
"""

import re
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from ingpipe_acquisition.fetch import build_session
from ingpipe_acquisition.manifest import Target
from ingpipe_lib.logconfig import get_logger

__all__ = [
    "discover",
    "get_chapter_pdf_links",
    "get_manual_pages",
    "title_to_folder_name",
]

logger = get_logger(__name__)

# Matches CMS manual link text (e.g. "100", "100-01", "100-04").
_MANUAL_LINK_PATTERN = re.compile(r"^100(-\d{2})?$")

# Runs of any character that is not a lowercase letter or digit collapse to a
# single underscore when slugifying a title or a path segment.
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def title_to_folder_name(page_title: str, manual_key: str) -> str:
    """Convert a CMS page title and manual key into a folder-safe name.

    Builds a prefix from the manual key (``"100-04"`` -> ``"pub_100_04"``),
    strips the common leading words ("Medicare", "Medicaid", "CMS") and a
    trailing "Manual", then slugifies the remainder. The 21 ingpipe_file_ingestion
    configs name these folders, so the derivation must stay stable.

    Args:
        page_title: The manual's title as scraped from the index table (e.g.
            "Medicare Claims Processing Manual").
        manual_key: The publication number (e.g. "100-04", "100").

    Returns:
        A folder-safe name (e.g. ``"pub_100_04_claims_processing"``).

    Raises:
        ValueError: If the title slugifies to nothing. The old code fell back
            to the manual key here, which produced a folder name
            (``pub_100_04_100_04``) that no ingest config references -- the
            manual downloaded into a directory nothing reads. Failing the
            target is the honest outcome.
    """
    prefix = "pub_" + manual_key.replace("-", "_")

    name = page_title
    for strip_prefix in ("Medicare ", "Medicaid ", "CMS "):
        if name.startswith(strip_prefix):
            name = name[len(strip_prefix) :]
    if name.endswith(" Manual"):
        name = name[: -len(" Manual")]

    slug = _NON_ALNUM_RE.sub("_", name.lower()).strip("_")
    if not slug:
        raise ValueError(
            f"Manual {manual_key} has no usable title (got {page_title!r}), so its "
            "folder name cannot be derived. The index page's title table is the "
            "source of these names and every downstream ingest config depends on "
            "them, so a missing title is a failure rather than a fallback."
        )
    return f"{prefix}_{slug}"


def _extract_table_titles(soup: BeautifulSoup) -> dict[str, str]:
    """Extract manual titles from the IOMs index page table.

    The index page carries a table with "Publication #" and "Title" columns;
    each title cell holds a responsive label followed by a div with the manual
    name.

    Args:
        soup: The parsed index page.

    Returns:
        A mapping of manual key (e.g. ``"100-04"``) to its title string.
    """
    titles: dict[str, str] = {}
    table = soup.find("table")
    if not table:
        logger.warning("No table found on the index page for title extraction")
        return titles

    for row in table.find_all("tr")[1:]:  # skip the header row
        cells = row.find_all("td")
        if len(cells) < 2:
            continue

        link = cells[0].find("a")
        if not link:
            continue
        manual_key = link.get_text(strip=True)

        title_div = cells[1].find("div")
        title_text = (title_div or cells[1]).get_text(strip=True)
        # Strip the "Title" prefix contributed by the responsive label element.
        if title_text.startswith("Title"):
            title_text = title_text[len("Title") :]

        if manual_key and title_text:
            titles[manual_key] = title_text

    logger.info(f"Extracted {len(titles)} manual titles from the index page table")
    return titles


def get_manual_pages(index_url: str, *, session: requests.Session) -> list[dict[str, str]]:
    """Scrape the IOMs index page and return every manual it references.

    Args:
        index_url: The full URL of the CMS IOMs index page.
        session: The shared session carrying the retry policy and timeout.

    Returns:
        One dict per unique manual with keys ``path`` (the relative URL),
        ``manual_key``, ``page_title``, and ``folder_name``.

    Raises:
        requests.RequestException: If the index page cannot be fetched.
        ValueError: If a discovered manual has no title in the index table
            (see :func:`title_to_folder_name`).
    """
    logger.info(f"Fetching the IOMs index page: {index_url}")
    with session.get(index_url) as response:
        response.raise_for_status()
        markup = response.text
    logger.debug(f"Index page length: {len(markup):,} characters")

    soup = BeautifulSoup(markup, "html.parser")
    table_titles = _extract_table_titles(soup)

    # Auto-discover manuals by matching link text against the publication
    # pattern, keeping the first occurrence of each key.
    manuals: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    for link in soup.find_all("a", href=True):
        # BeautifulSoup types attributes as str | AttributeValueList; an <a>
        # href is always a single string at runtime.
        href = str(link["href"])
        link_text = link.get_text(strip=True)
        if not link_text or not _MANUAL_LINK_PATTERN.match(link_text):
            continue
        if link_text in seen_keys:
            continue
        seen_keys.add(link_text)
        manuals.append({"path": href, "manual_key": link_text})

    logger.info(f"Found {len(manuals)} unique manual page link(s) on the index")

    for manual in manuals:
        manual_key = manual["manual_key"]
        page_title = table_titles.get(manual_key)
        if page_title is None:
            raise ValueError(
                f"Manual {manual_key} appears on the index page but has no row in the "
                "title table. The folder names every downstream ingest config uses are "
                "derived from those titles, so the run stops rather than inventing one."
            )
        manual["page_title"] = page_title
        manual["folder_name"] = title_to_folder_name(page_title, manual_key)
        logger.info(f"Manual {manual_key}: title={page_title!r}, folder={manual['folder_name']!r}")

    return manuals


def get_chapter_pdf_links(
    manual_url: str, *, session: requests.Session, exclude_patterns: list[str] | None = None
) -> list[str]:
    """Scrape a manual's detail page for its chapter PDF URLs.

    Args:
        manual_url: The full URL of the manual's detail page.
        session: The shared session carrying the retry policy and timeout.
        exclude_patterns: Case-insensitive substrings that disqualify a URL
            (e.g. ``["crosswalk"]``). None or empty means no exclusions.

    Returns:
        The absolute PDF URLs, deduplicated and sorted for deterministic
        ordering (which in turn makes the collision-disambiguation below
        deterministic).

    Raises:
        requests.RequestException: If the manual page cannot be fetched.
    """
    logger.info(f"Fetching manual detail page: {manual_url}")
    with session.get(manual_url) as response:
        response.raise_for_status()
        markup = response.text

    soup = BeautifulSoup(markup, "html.parser")
    # Scope the search to <article> so navigation and sidebar PDFs (e.g.
    # agent/broker-help-desks.pdf, present outside the article on every page)
    # are not mistaken for chapters.
    content = soup.find("article") or soup
    if content is soup:
        logger.warning(f"No <article> element on {manual_url}; searching the whole page")

    pdf_links: list[str] = []
    for link in content.find_all("a", href=True):
        href = str(link["href"])
        if not href.lower().endswith(".pdf"):
            continue
        if exclude_patterns and any(p.lower() in href.lower() for p in exclude_patterns):
            logger.debug(f"Excluding PDF (matched an exclude pattern): {href}")
            continue
        pdf_links.append(urljoin(manual_url, href))

    unique_links = sorted(set(pdf_links))
    logger.info(f"Found {len(unique_links)} chapter PDF link(s)")
    return unique_links


def _matches_manual_filter(manual_key: str, manual_filter: list[str]) -> bool:
    """Check a manual key against the configured filter list.

    Args:
        manual_key: The manual key (e.g. ``"100"``, ``"100-04"``).
        manual_filter: The identifiers from the config's ``manuals`` key. The
            shorthand ``"100-introduction"`` matches the key ``"100"``.

    Returns:
        True when the manual passes the filter, or the filter is empty.
    """
    if not manual_filter:
        return True
    if manual_key in manual_filter:
        return True
    return manual_key == "100" and "100-introduction" in manual_filter


def _chapter_filename(pdf_url: str, used: set[str]) -> str:
    """Derive a unique destination filename for one chapter URL.

    The bare basename is used when it is free -- today's chapter names, which
    the 21 ingpipe_file_ingestion configs reference by name, therefore do not change.
    When two chapter URLs under different directories share a basename, the
    URL's parent path segment is folded in rather than one chapter silently
    overwriting the other, which is what the old code did.

    Args:
        pdf_url: The absolute chapter PDF URL.
        used: The filenames already claimed within this manual's folder;
            updated in place.

    Returns:
        A filename unique within the manual's folder.
    """
    path_parts = [part for part in urlparse(pdf_url).path.split("/") if part]
    basename = path_parts[-1] if path_parts else "chapter.pdf"

    candidate = basename
    if candidate in used and len(path_parts) > 1:
        parent = _NON_ALNUM_RE.sub("_", path_parts[-2].lower()).strip("_")
        candidate = f"{parent}_{basename}" if parent else basename
    # A parent segment that also collides (or does not exist) falls back to an
    # ordinal, so two URLs can never resolve to one destination.
    ordinal = 2
    while candidate in used:
        stem, _, suffix = basename.rpartition(".")
        candidate = f"{stem}_{ordinal}.{suffix}" if stem else f"{basename}_{ordinal}"
        ordinal += 1

    used.add(candidate)
    return candidate


def discover(config: dict) -> Iterator[Target]:
    """Yield every CMS IOM chapter PDF target the config selects.

    Args:
        config: The parsed acquisition config. Reads ``base_url`` and
            ``index_path`` from the ``[discovery]`` table, plus the optional
            ``manuals`` filter and ``exclude_patterns``; the polite delay and
            the transport settings come from the shared runner keys.

    Yields:
        One :class:`~ingpipe_acquisition.manifest.Target` per chapter PDF, with a
        destination of ``<manual folder>/<chapter filename>`` and the manual
        folder as its group.

    Raises:
        ValueError: If the config is missing ``discovery.base_url`` or
            ``discovery.index_path``, or if a discovered manual has no title
            in the index table.
        requests.RequestException: If the index page or a manual page cannot
            be fetched. Discovery is a whole-run precondition -- unlike a
            single chapter's download, a failure here means the target set is
            unknown, so the run stops.
    """
    discovery = config.get("discovery")
    if not isinstance(discovery, dict):
        raise ValueError("Config table '[discovery]' is required for the cms_iom source")

    base_url = discovery.get("base_url")
    index_path = discovery.get("index_path")
    if not isinstance(base_url, str) or not base_url:
        raise ValueError("Config key 'discovery.base_url' must be a non-empty string")
    if not isinstance(index_path, str) or not index_path:
        raise ValueError("Config key 'discovery.index_path' must be a non-empty string")

    manual_filter: list[str] = discovery.get("manuals", [])
    exclude_patterns: list[str] = discovery.get("exclude_patterns", [])
    delay = float(config.get("request_delay_seconds", 1.0))
    http = config.get("http", {})

    with build_session(
        retries=int(http.get("retries", 3)),
        backoff_factor=float(http.get("backoff_factor", 0.5)),
        timeout=float(http.get("timeout", 60.0)),
    ) as session:
        manuals = get_manual_pages(urljoin(base_url, index_path), session=session)

        if manual_filter:
            manuals = [m for m in manuals if _matches_manual_filter(m["manual_key"], manual_filter)]
            logger.info(f"Filtered to {len(manuals)} manual(s) matching {manual_filter}")

        for manual in manuals:
            # Polite delay before every manual page request, including the
            # first -- the index page was just fetched from the same host.
            time.sleep(delay)

            folder_name = manual["folder_name"]
            manual_url = urljoin(base_url, manual["path"])
            pdf_links = get_chapter_pdf_links(
                manual_url, session=session, exclude_patterns=exclude_patterns
            )

            used: set[str] = set()
            for pdf_url in pdf_links:
                filename = _chapter_filename(pdf_url, used)
                yield Target(
                    url=pdf_url,
                    destination=Path(folder_name) / filename,
                    group=folder_name,
                )
