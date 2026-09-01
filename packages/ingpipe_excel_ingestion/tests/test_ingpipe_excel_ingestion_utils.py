"""Unit tests for the shared ingpipe_excel_ingestion utilities."""


import pytest
from ingpipe_excel_ingestion._utils import (
    MAX_IDENTIFIER_LENGTH,
    compute_source_hash,
    deduplicate_columns,
    get_engine,
    make_collection_path,
    normalize_column_name,
    validate_sql_identifier,
)
from ingpipe_lib.logconfig import setup_logging
from ingpipe_lib.paths import resolve_log_dir

setup_logging(log_dir=resolve_log_dir("ingpipe_excel_ingestion/unit_tests"), log_name="test_utils")


# ---------------------------------------------------------------------------
# normalize_column_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header, index, expected",
    [
        # col_ prefix makes a reserved word safe
        ("window", 0, "col_window"),
        # col_ prefix makes a leading-digit header safe
        ("2021 Q1", 0, "col_2021_q1"),
        # slashes collapse to a single underscore
        ("CPT/HCPCS", 0, "col_cpt_hcpcs"),
        # parens and spaces
        ("Rate (per 1000)", 0, "col_rate_per_1000"),
        # already-snake header
        ("already_snake", 0, "col_already_snake"),
        # trailing/leading punctuation stripped
        ("Item #", 0, "col_item"),
        ("  Leading Spaces  ", 0, "col_leading_spaces"),
    ],
)
def test_normalize_column_name_snake_and_prefix(
    header: str, index: int, expected: str
) -> None:
    """Headers are snake_cased and prefixed with col_."""
    assert normalize_column_name(header, index) == expected


@pytest.mark.parametrize("header, index", [("", 3), ("###", 7), ("   ", 2)])
def test_normalize_column_name_empty_fallback(header: str, index: int) -> None:
    """An empty/degenerate header falls back to col_<index>."""
    assert normalize_column_name(header, index) == f"col_{index}"


def test_normalize_column_name_caps_long_header() -> None:
    """An over-long header is capped at the Postgres identifier limit.

    Postgres silently truncates identifiers past 63 bytes; without an explicit
    cap the generated name and the stored name diverge, breaking ADD COLUMN
    reconciliation (the real qpp_cm ICD-10 Service Assignment Decision column).
    """
    header = "ICD-10 CM 3-Digit Diagnosis Code Service Assignment Decision"
    result = normalize_column_name(header, 0)
    assert len(result) <= MAX_IDENTIFIER_LENGTH
    # Capping must not leave a trailing underscore.
    assert not result.endswith("_")
    assert result.startswith("col_")


def test_normalize_column_name_cap_boundary_strips_trailing_underscore() -> None:
    """When the 63-char cut lands on an underscore, it is stripped.

    The cap exists because Postgres silently truncates identifiers at 63 bytes;
    a cut that left a trailing underscore would mismatch the stored name. This
    header snakes to an underscore exactly at the cut point, forcing the
    boundary ``rstrip('_')`` to fire (the all-``a`` cap test never exercises it).
    """
    # "a"*58 + " x" -> "col_" + "a"*58 + "_x"; the cut at 63 lands on the "_".
    result = normalize_column_name("a" * 58 + " x", 0)
    assert result == "col_" + "a" * 58
    assert len(result) == 62
    assert not result.endswith("_")


# ---------------------------------------------------------------------------
# deduplicate_columns
# ---------------------------------------------------------------------------


def test_deduplicate_columns_no_collision() -> None:
    """Unique names pass through unchanged."""
    assert deduplicate_columns(["col_a", "col_b"]) == ["col_a", "col_b"]


def test_deduplicate_columns_suffixes_collisions() -> None:
    """Collisions get numeric suffixes starting at _2."""
    assert deduplicate_columns(["col_question", "col_question"]) == [
        "col_question",
        "col_question_2",
    ]


def test_deduplicate_columns_three_way() -> None:
    """A three-way collision yields _2 and _3."""
    assert deduplicate_columns(["col_name", "col_name", "col_name"]) == [
        "col_name",
        "col_name_2",
        "col_name_3",
    ]


def test_deduplicate_columns_truncation_collision_stays_unique() -> None:
    """Two names that truncate to the same 63 chars get distinct identifiers.

    After capping, two distinct headers can normalize to the same capped name;
    deduplication must keep them unique AND keep the suffixed name within the
    identifier limit (so Postgres does not re-truncate it into a re-collision).
    """
    capped = "col_" + "a" * (MAX_IDENTIFIER_LENGTH - 4)  # exactly 63 chars
    result = deduplicate_columns([capped, capped])
    assert result[0] == capped
    assert result[1] != capped
    assert len(result[1]) <= MAX_IDENTIFIER_LENGTH
    assert len(set(result)) == 2


def test_deduplicate_columns_suffix_does_not_resurrect_collision() -> None:
    """A generated suffix that clashes with an existing name is skipped."""
    # col_x_2 already present, so the second col_x must become col_x_3.
    assert deduplicate_columns(["col_x", "col_x_2", "col_x"]) == [
        "col_x",
        "col_x_2",
        "col_x_3",
    ]


def test_deduplicate_columns_input_matching_prior_suffix() -> None:
    """An input equal to an already-generated suffix gets its own suffix.

    Regression: a name that enters `seen` as a generated candidate (here
    `col_x_2`, produced from the second `col_x`) has no `counts` entry, so the
    collision branch must seed the counter rather than `+=` it — otherwise a
    later identical input raises KeyError.
    """
    assert deduplicate_columns(["col_x", "col_x", "col_x_2"]) == [
        "col_x",
        "col_x_2",
        "col_x_2_2",
    ]


# ---------------------------------------------------------------------------
# get_engine
# ---------------------------------------------------------------------------


def test_get_engine_percent_encodes_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Credentials with URL-reserved characters are percent-encoded in the URL."""
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.setenv("POSTGRES_USER", "user@corp")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss:word/!")

    engine = get_engine("ingestion_test")

    rendered = engine.url.render_as_string(hide_password=False)
    # The raw reserved characters must not appear unencoded in the netloc.
    assert "p@ss:word/!" not in rendered
    assert "%40" in rendered  # encoded '@'
    assert engine.url.database == "ingestion_test"
    assert engine.url.username == "user@corp"  # round-trips after decode


@pytest.mark.parametrize(
    "missing", ["POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD"]
)
def test_get_engine_missing_env_raises(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """A missing POSTGRES_* variable raises ValueError."""
    for var in (
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
    ):
        monkeypatch.setenv(var, "x" if var != "POSTGRES_PORT" else "5432")
    monkeypatch.delenv(missing, raising=False)

    with pytest.raises(ValueError, match="Missing Postgres environment variable"):
        get_engine("ingestion_test")


# ---------------------------------------------------------------------------
# validate_sql_identifier (re-exported; verify fullmatch semantics)
# ---------------------------------------------------------------------------


def test_validate_sql_identifier_accepts_safe() -> None:
    """A safe identifier passes through unchanged."""
    assert validate_sql_identifier("col_window", "label") == "col_window"


def test_validate_sql_identifier_rejects_trailing_newline() -> None:
    """fullmatch (not match) rejects a trailing newline."""
    with pytest.raises(ValueError, match="Unsafe SQL identifier"):
        validate_sql_identifier("public\n", "db_schema")


# ---------------------------------------------------------------------------
# make_collection_path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename, sheet, expected",
    [
        # the canonical qpp_cm shape: extension dropped, hyphens -> underscores
        ("2025-codes-list-aki.xlsx", "Triggers", "2025_codes_list_aki.triggers"),
        # spaces/caps in both labels
        ("Example_Workbook.xlsx", "Sheet A", "example_workbook.sheet_a"),
        # punctuation collapses; underscores preserved
        ("Appendix B - Data.xlsx", "HCPCS_Surgery", "appendix_b_data.hcpcs_surgery"),
        # a leading-digit label is a valid ltree label (no prefix needed)
        ("2021 Q1.xlsx", "Tab", "2021_q1.tab"),
    ],
)
def test_make_collection_path_derives_and_sanitizes(
    filename: str, sheet: str, expected: str
) -> None:
    """A derived path sanitizes the filename stem and sheet into two ltree labels."""
    assert make_collection_path(filename, sheet) == expected


def test_make_collection_path_override_validated_unchanged() -> None:
    """A valid authored override is returned unchanged (validate-not-sanitize)."""
    assert (
        make_collection_path("x.xlsx", "S", override="qpp_cm.aki.triggers")
        == "qpp_cm.aki.triggers"
    )


def test_make_collection_path_override_invalid_raises() -> None:
    """An invalid authored override raises rather than being rewritten."""
    with pytest.raises(ValueError, match="Invalid collection_path"):
        make_collection_path("x.xlsx", "S", override="Bad Path")


def test_make_collection_path_degenerate_name_raises() -> None:
    """A name whose label sanitizes to empty yields an invalid path and raises."""
    with pytest.raises(ValueError, match="Invalid collection_path"):
        make_collection_path("x.xlsx", "###")


def test_make_collection_path_prefix_is_prepended_to_a_derived_path() -> None:
    """A prefix puts derived sheets under a schema-scoped branch of the tree.

    Without it every path is a bare ``<stem>.<leaf>``, so a corpus filter like
    ``qpp_cm.%`` matches no sheet at all -- silently dropping the whole leg from
    a schema-scoped query rather than reporting an error.
    """
    assert (
        make_collection_path(
            "2025-12-py2026-codes-list-aki.xlsx",
            "Triggers",
            None,
            "qpp_cm.2026_cost_measure_codes_lists",
        )
        == "qpp_cm.2026_cost_measure_codes_lists.2025_12_py2026_codes_list_aki.triggers"
    )


def test_make_collection_path_prefix_is_ignored_when_an_override_is_present() -> None:
    """An authored override already states the full path, so it is never prefixed."""
    assert (
        make_collection_path("x.xlsx", "S", "qpp_cm.aki.triggers", "qpp_cm.2026_lists")
        == "qpp_cm.aki.triggers"
    )


def test_make_collection_path_invalid_prefix_raises() -> None:
    """A malformed prefix fails at derivation rather than yielding a bad ltree."""
    with pytest.raises(ValueError, match="Invalid collection_path"):
        make_collection_path("x.xlsx", "Sheet", None, "Bad Prefix")


def test_make_collection_path_prefixed_degenerate_name_still_raises() -> None:
    """A prefix cannot rescue a sheet name that sanitizes to nothing."""
    with pytest.raises(ValueError, match="Invalid collection_path"):
        make_collection_path("x.xlsx", "###", None, "qpp_cm.lists")


# ---------------------------------------------------------------------------
# compute_source_hash
# ---------------------------------------------------------------------------


def test_compute_source_hash_deterministic_and_in_uint64_range() -> None:
    """The hash is deterministic and within the unsigned 64-bit range."""
    h = compute_source_hash(["Code: A1", "Code: B2"])
    assert 0 <= h < 2**64
    assert compute_source_hash(["Code: A1", "Code: B2"]) == h


def test_compute_source_hash_changes_with_content() -> None:
    """A change in any row_text flips the hash (the change-detection signal)."""
    assert compute_source_hash(["Code: A1"]) != compute_source_hash(["Code: A2"])


def test_compute_source_hash_newline_join_is_load_bearing() -> None:
    """Row order and the newline separator both affect the hash.

    The ``"\\n".join`` contract is load-bearing for change detection: a wrong
    delimiter would still be deterministic and in range, so pin that order
    matters and that two rows are distinct from one concatenated row.
    """
    assert compute_source_hash(["A", "B"]) != compute_source_hash(["B", "A"])
    assert compute_source_hash(["A", "B"]) != compute_source_hash(["AB"])
