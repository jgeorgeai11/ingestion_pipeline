"""Unit tests for the cms_iom acquisition discoverer.

Carried over from test_download_cms_iom.py and retargeted at ``discover``.
Two cases changed rather than moved, and both are deliberate:

  - ``test_fallback_when_no_table_title`` asserted that the degraded folder
    name ``pub_100_04_100_04`` was CORRECT. It pinned a silent-degradation
    path: no ingest config references that name, so the manual downloaded into
    a directory nothing reads. The replacement asserts the run now fails.
  - The old ``TestDownloadFile`` cases are gone with the duplicated
    ``download_file`` they tested; the one remaining implementation is covered
    by the acquisition package's ``test_fetch.py``, including the atomicity and
    retry behavior neither copy had.
"""

from pathlib import Path

import pytest
import requests
from bs4 import BeautifulSoup
from ingpipe_acquisition.manifest import Target
from ingpipe_acquisition.runner import run_acquisition
from policy_db_acquisition.cms_iom.discover import (
    _extract_table_titles,
    discover,
    get_chapter_pdf_links,
    get_manual_pages,
    title_to_folder_name,
)

# ---------------------------------------------------------------------------
# Sample HTML fixtures
# ---------------------------------------------------------------------------

SAMPLE_MANUAL_PAGE_HTML = """
<html>
<body>
<article>
    <a href="/regulations-and-guidance/guidance/manuals/downloads/clm104c01.pdf">Chapter 1</a>
    <a href="/regulations-and-guidance/guidance/manuals/downloads/clm104c02.pdf">Chapter 2</a>
    <a href="/regulations-and-guidance/guidance/manuals/downloads/clm104c03.pdf">Chapter 3</a>
    <a href="/files/document/clm104-crosswalk.pdf">Crosswalk Document</a>
    <a href="/files/document/clm104-Crosswalk-2024.pdf">Crosswalk 2024</a>
    <a href="/regulations-and-guidance/guidance/manuals/downloads/clm104c04.pdf">Chapter 4</a>
    <a href="/about-us/contact-us">Contact Page</a>
</article>
</body>
</html>
"""

SAMPLE_INDEX_PAGE_HTML = """
<html>
<body>
<table>
<tr><th>Publication #</th><th>Title</th></tr>
<tr>
    <td><a href="/ioms-items/cms018912">100</a></td>
    <td><div>Introduction</div></td>
</tr>
<tr>
    <td><a href="/ioms-items/cms019033">100-01</a></td>
    <td><div>Medicare General Information, Eligibility and Entitlement Manual</div></td>
</tr>
<tr>
    <td><a href="/ioms-items/cms019034">100-04</a></td>
    <td><div>Medicare Claims Processing Manual</div></td>
</tr>
<tr>
    <td><a href="/ioms-items/cms019035">100-11</a></td>
    <td><div>Programs of All-Inclusive Care for the Elderly (PACE)Manual</div></td>
</tr>
</table>
<a href="/about-us/contact-us">Contact Page</a>
<a href="/ioms-items/cms019036">Not A Manual</a>
<a href="/ioms-items/cms019037">200-01</a>
</body>
</html>
"""

SAMPLE_INDEX_PAGE_NO_TABLE_HTML = """
<html>
<body>
<div>
    <a href="/ioms-items/cms019034">100-04</a>
</div>
</body>
</html>
"""

INDEX_ONE_MANUAL_HTML = """
<html><body>
<table>
<tr><th>Publication #</th><th>Title</th></tr>
<tr>
    <td><a href="/ioms/100-04">100-04</a></td>
    <td><div>Medicare Claims Processing Manual</div></td>
</tr>
</table>
</body></html>
"""


class _FakeResponse:
    """A minimal response standing in for ``requests.Response``."""

    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        """Succeed: these fixtures stand in for 200 responses."""

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info) -> None:
        return None


class _FakeSession:
    """A session returning canned markup per URL substring."""

    def __init__(self, pages: dict[str, str], default: str = "<html></html>") -> None:
        self.pages = pages
        self.default = default
        self.requested: list[str] = []

    def get(self, url: str, **kwargs) -> _FakeResponse:
        self.requested.append(url)
        # Longest fragment first, so "/ioms/100-04" wins over "/ioms".
        for fragment in sorted(self.pages, key=len, reverse=True):
            if fragment in url:
                return _FakeResponse(self.pages[fragment])
        return _FakeResponse(self.default)

    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, *exc_info) -> None:
        return None


def _config(**overrides) -> dict:
    """Build an acquisition config for the cms_iom discoverer."""
    discovery = {
        "base_url": "https://www.cms.gov",
        "index_path": "/ioms",
        "exclude_patterns": ["crosswalk"],
        "manuals": [],
    }
    discovery.update(overrides.pop("discovery", {}))
    config = {
        "output_dir": "data/input/cms_iom/2026-06-11",
        "request_delay_seconds": 0.0,
        "min_targets": 1,
        "http": {"retries": 0, "backoff_factor": 0.0, "timeout": 1.0},
        "discovery": discovery,
    }
    config.update(overrides)
    return config


@pytest.fixture
def no_sleep(mocker):
    """Remove the polite delay so the scrape tests run instantly."""
    return mocker.patch("policy_db_acquisition.cms_iom.discover.time.sleep")


def _patch_session(mocker, session: _FakeSession) -> _FakeSession:
    """Make the discoverer use the given fake session."""
    mocker.patch(
        "policy_db_acquisition.cms_iom.discover.build_session", return_value=session
    )
    return session


# ---------------------------------------------------------------------------
# title_to_folder_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("page_title", "manual_key", "expected"),
    [
        ("Introduction", "100", "pub_100_introduction"),
        ("Medicare Claims Processing Manual", "100-04", "pub_100_04_claims_processing"),
        ("Medicare Benefit Policy Manual", "100-02", "pub_100_02_benefit_policy"),
        ("Medicare Secondary Payer Manual", "100-05", "pub_100_05_secondary_payer"),
        (
            "Medicare Financial Management Manual",
            "100-06",
            "pub_100_06_financial_management",
        ),
        (
            "Medicare General Information, Eligibility, and Entitlement Manual",
            "100-01",
            "pub_100_01_general_information_eligibility_and_entitlement",
        ),
        ("Medicare State Operations Manual", "100-07", "pub_100_07_state_operations"),
        ("Medicare Program Integrity Manual", "100-08", "pub_100_08_program_integrity"),
        (
            "Medicare National Coverage Determinations (NCD) Manual",
            "100-03",
            "pub_100_03_national_coverage_determinations_ncd",
        ),
        ("Medicare PACE Manual", "100-11", "pub_100_11_pace"),
        ("CMS Quality Reporting Manual", "100-22", "pub_100_22_quality_reporting"),
        ("Medicaid Program Integrity Manual", "100-15", "pub_100_15_program_integrity"),
        (
            "Prescription Drug Benefit Manual",
            "100-18",
            "pub_100_18_prescription_drug_benefit",
        ),
    ],
)
def test_title_to_folder_name_derives_known_cms_titles(page_title, manual_key, expected):
    """The folder names the 21 ingpipe_file_ingestion configs reference stay stable."""
    assert title_to_folder_name(page_title, manual_key) == expected


def test_title_to_folder_name_raises_when_the_title_slugifies_to_nothing():
    """A degenerate title fails rather than degrading to the key alone.

    The old code returned the bare prefix here, orphaning the manual in a
    folder no ingest config names.
    """
    with pytest.raises(ValueError, match="no usable title"):
        title_to_folder_name("Medicare ", "100-99")


# ---------------------------------------------------------------------------
# _extract_table_titles
# ---------------------------------------------------------------------------


def test_extract_table_titles_reads_the_index_table():
    """Titles come from the index page's publication table."""
    titles = _extract_table_titles(BeautifulSoup(SAMPLE_INDEX_PAGE_HTML, "html.parser"))

    assert titles["100"] == "Introduction"
    assert titles["100-04"] == "Medicare Claims Processing Manual"
    assert titles["100-11"] == "Programs of All-Inclusive Care for the Elderly (PACE)Manual"
    assert len(titles) == 4


def test_extract_table_titles_returns_empty_without_a_table():
    """A page with no table yields no titles, which the caller then rejects."""
    assert _extract_table_titles(
        BeautifulSoup(SAMPLE_INDEX_PAGE_NO_TABLE_HTML, "html.parser")
    ) == {}


def test_extract_table_titles_strips_the_responsive_label_prefix():
    """The 'Title' label injected by the responsive markup is removed."""
    html = """
    <table>
    <tr><th>Publication #</th><th>Title</th></tr>
    <tr>
        <td><a href="/ioms/100-04">100-04</a></td>
        <td><div>TitleMedicare Claims Processing Manual</div></td>
    </tr>
    </table>
    """

    titles = _extract_table_titles(BeautifulSoup(html, "html.parser"))

    assert titles["100-04"] == "Medicare Claims Processing Manual"


# ---------------------------------------------------------------------------
# get_manual_pages
# ---------------------------------------------------------------------------


def test_get_manual_pages_discovers_manuals_by_pattern():
    """Only link text matching ^100(-\\d{2})?$ is a manual."""
    session = _FakeSession({"/ioms": SAMPLE_INDEX_PAGE_HTML})

    manuals = get_manual_pages("https://www.cms.gov/ioms", session=session)

    assert {m["manual_key"] for m in manuals} == {"100", "100-01", "100-04", "100-11"}


def test_get_manual_pages_enriches_with_the_title_and_folder():
    """Each manual carries the title it was named by and its folder."""
    session = _FakeSession({"/ioms": SAMPLE_INDEX_PAGE_HTML})

    manuals = get_manual_pages("https://www.cms.gov/ioms", session=session)
    manual = next(m for m in manuals if m["manual_key"] == "100-04")

    assert manual["page_title"] == "Medicare Claims Processing Manual"
    assert manual["folder_name"] == "pub_100_04_claims_processing"


def test_get_manual_pages_deduplicates_repeated_links():
    """The same manual linked twice is one manual."""
    html = """
    <html><body>
    <table>
    <tr><th>Publication #</th><th>Title</th></tr>
    <tr><td><a href="/ioms/100-04">100-04</a></td>
        <td><div>Medicare Claims Processing Manual</div></td></tr>
    </table>
    <a href="/ioms/100-04">100-04</a>
    <a href="/ioms/100-04-dup">100-04</a>
    </body></html>
    """
    session = _FakeSession({"/ioms": html})

    assert len(get_manual_pages("https://www.cms.gov/ioms", session=session)) == 1


def test_get_manual_pages_fails_when_a_manual_has_no_table_title():
    """Inverted from the old test, which asserted the degraded name was right.

    ``pub_100_04_100_04`` is referenced by nothing downstream, so a manual
    landing there is lost. Discovery now stops instead.
    """
    session = _FakeSession({"/ioms": SAMPLE_INDEX_PAGE_NO_TABLE_HTML})

    with pytest.raises(ValueError, match="no row in the title table"):
        get_manual_pages("https://www.cms.gov/ioms", session=session)


# ---------------------------------------------------------------------------
# get_chapter_pdf_links
# ---------------------------------------------------------------------------


def test_get_chapter_pdf_links_extracts_and_excludes():
    """PDF links are absolute, sorted, and filtered by the exclude patterns."""
    session = _FakeSession({"/manuals/100-04": SAMPLE_MANUAL_PAGE_HTML})

    links = get_chapter_pdf_links(
        "https://www.cms.gov/manuals/100-04", session=session, exclude_patterns=["crosswalk"]
    )

    assert len(links) == 4
    assert all(url.startswith("https://") and url.endswith(".pdf") for url in links)
    assert links == sorted(links)


def test_get_chapter_pdf_links_without_exclusions_returns_every_pdf():
    """No exclusions means every PDF inside the article is a chapter."""
    session = _FakeSession({"/manuals/100-04": SAMPLE_MANUAL_PAGE_HTML})

    assert len(get_chapter_pdf_links("https://www.cms.gov/manuals/100-04", session=session)) == 6


def test_get_chapter_pdf_links_excludes_case_insensitively():
    """CROSSWALK and CrossWalk are excluded by the pattern 'crosswalk'."""
    html = """
    <html><body><article>
        <a href="/downloads/chapter1.pdf">Ch1</a>
        <a href="/downloads/CROSSWALK_doc.pdf">Crosswalk</a>
        <a href="/downloads/CrossWalk_v2.pdf">CrossWalk</a>
    </article></body></html>
    """
    session = _FakeSession({"/manuals/100-04": html})

    links = get_chapter_pdf_links(
        "https://www.cms.gov/manuals/100-04", session=session, exclude_patterns=["crosswalk"]
    )

    assert len(links) == 1
    assert links[0].endswith("chapter1.pdf")


def test_get_chapter_pdf_links_deduplicates():
    """The same chapter linked twice is one chapter."""
    html = """
    <html><body><article>
        <a href="/downloads/ch1.pdf">Chapter 1</a>
        <a href="/downloads/ch1.pdf">Chapter 1 Again</a>
        <a href="/downloads/ch2.pdf">Chapter 2</a>
    </article></body></html>
    """
    session = _FakeSession({"/manuals/100-04": html})

    assert len(get_chapter_pdf_links("https://www.cms.gov/manuals/100-04", session=session)) == 2


def test_get_chapter_pdf_links_falls_back_to_the_whole_page_without_an_article():
    """A page with no <article> is searched entirely, with a warning."""
    html = '<html><body><a href="/downloads/ch1.pdf">Chapter 1</a></body></html>'
    session = _FakeSession({"/manuals/100-04": html})

    assert len(get_chapter_pdf_links("https://www.cms.gov/manuals/100-04", session=session)) == 1


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------


def test_discover_yields_a_target_per_chapter(mocker, no_sleep):
    """Each chapter becomes a target under its manual's folder."""
    _patch_session(
        mocker,
        _FakeSession(
            {"/ioms": INDEX_ONE_MANUAL_HTML, "/ioms/100-04": SAMPLE_MANUAL_PAGE_HTML}
        ),
    )

    targets = list(discover(_config()))

    assert len(targets) == 4
    assert targets[0] == Target(
        url="https://www.cms.gov/regulations-and-guidance/guidance/manuals/downloads/clm104c01.pdf",
        destination=Path("pub_100_04_claims_processing/clm104c01.pdf"),
        group="pub_100_04_claims_processing",
    )


def test_discover_applies_the_manual_filter(mocker, no_sleep):
    """The 100-introduction shorthand selects the manual keyed '100'."""
    index = """
    <html><body>
    <table>
    <tr><th>Publication #</th><th>Title</th></tr>
    <tr><td><a href="/ioms/100">100</a></td><td><div>Introduction</div></td></tr>
    <tr><td><a href="/ioms/100-04">100-04</a></td>
        <td><div>Medicare Claims Processing Manual</div></td></tr>
    </table>
    </body></html>
    """
    _patch_session(
        mocker,
        _FakeSession(
            {
                "/ioms/100-04": SAMPLE_MANUAL_PAGE_HTML,
                "/ioms/100": SAMPLE_MANUAL_PAGE_HTML,
                "/ioms": index,
            }
        ),
    )

    targets = list(discover(_config(discovery={"manuals": ["100-introduction"]})))

    assert {target.group for target in targets} == {"pub_100_introduction"}


def test_discover_gives_two_chapters_sharing_a_basename_distinct_destinations(
    mocker, no_sleep
):
    """Regression guard: a basename collision no longer drops a chapter.

    The old code keyed each destination on the URL's bare basename, so the
    second of these two URLs silently overwrote the first.
    """
    manual = """
    <html><body><article>
        <a href="/downloads/2024/chapter1.pdf">Chapter 1 (2024)</a>
        <a href="/downloads/2025/chapter1.pdf">Chapter 1 (2025)</a>
    </article></body></html>
    """
    _patch_session(
        mocker, _FakeSession({"/ioms/100-04": manual, "/ioms": INDEX_ONE_MANUAL_HTML})
    )

    targets = list(discover(_config()))

    assert len(targets) == 2
    assert len({target.destination for target in targets}) == 2


def test_discover_yields_nothing_when_the_link_pattern_matches_nothing(mocker, no_sleep):
    """A markup change that breaks the pattern resolves to zero targets."""
    _patch_session(mocker, _FakeSession({"/ioms": "<html><body></body></html>"}))

    assert list(discover(_config())) == []


def test_the_runner_fails_the_run_when_discovery_yields_nothing(mocker, no_sleep, tmp_path):
    """Regression guard for the silent zero-discovery.

    Previously this logged ``Found 0 manual page links`` at INFO, downloaded
    nothing, and exited 0. With a floor in config it is a failed run.
    """
    instance = tmp_path / "instance"
    (instance / "config").mkdir(parents=True)
    (instance / "pyproject.toml").write_text("[project]\nname='t'\n", encoding="utf-8")
    config_path = instance / "config" / "download_cms_iom.toml"
    config_path.write_text("", encoding="utf-8")

    _patch_session(mocker, _FakeSession({"/ioms": "<html><body></body></html>"}))
    fetch = mocker.patch("ingpipe_acquisition.runner.fetch")

    with pytest.raises(ValueError, match="below the configured min_targets"):
        run_acquisition(_config(min_targets=100), config_path, discover=discover)

    fetch.assert_not_called()


@pytest.mark.parametrize(
    ("discovery", "expected"),
    [
        ({"base_url": None}, "'discovery.base_url' must be a non-empty string"),
        ({"base_url": ""}, "'discovery.base_url' must be a non-empty string"),
        ({"index_path": None}, "'discovery.index_path' must be a non-empty string"),
    ],
)
def test_discover_rejects_a_malformed_discovery_table(discovery, expected):
    """The two required discovery keys are checked before any request."""
    with pytest.raises(ValueError, match=expected):
        list(discover(_config(discovery=discovery)))


def test_discover_requires_a_discovery_table():
    """A config with no [discovery] table cannot name the source."""
    with pytest.raises(ValueError, match=r"'\[discovery\]' is required"):
        list(discover({"output_dir": "d"}))


def test_a_failed_index_fetch_aborts_discovery(mocker, no_sleep):
    """Discovery failure is a whole-run failure: the target set is unknown."""

    class _FailingSession(_FakeSession):
        def get(self, url: str, **kwargs):
            raise requests.ConnectionError("index page unreachable")

    _patch_session(mocker, _FailingSession({}))

    with pytest.raises(requests.RequestException):
        list(discover(_config()))
