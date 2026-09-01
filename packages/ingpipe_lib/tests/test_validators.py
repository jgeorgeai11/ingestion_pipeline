"""Unit tests for the shared validators (ingpipe_lib.validators)."""

import pytest
from ingpipe_lib.validators import validate_collection_path, validate_sql_identifier


@pytest.mark.parametrize(
    "name",
    [
        "public",
        "cms_iom",
        "_private",
        "a",
        "_",
        "table1",
        "document_content",
        "a1_b2_c3",
    ],
)
def test_validate_sql_identifier_valid_returned_unchanged(name: str) -> None:
    assert validate_sql_identifier(name, "db_schema") == name


def test_validate_sql_identifier_rejects_trailing_newline() -> None:
    # Regression guard for fullmatch vs match: with re.match, the anchored
    # ``$`` would also match just before a trailing newline, so "public\n"
    # would pass validation and reach SQL.
    with pytest.raises(ValueError, match="Unsafe SQL identifier"):
        validate_sql_identifier("public\n", "db_schema")


@pytest.mark.parametrize(
    "name",
    [
        "Public",  # uppercase
        "PUBLIC",  # uppercase
        "1table",  # leading digit
        "my-table",  # dash
        "my table",  # space
        'my"table',  # double quote
        "my'table",  # single quote
        "table;drop",  # semicolon
        "",  # empty string
    ],
)
def test_validate_sql_identifier_invalid_raises_with_value_and_label(
    name: str,
) -> None:
    with pytest.raises(ValueError, match="Unsafe SQL identifier") as exc_info:
        validate_sql_identifier(name, "db_schema")
    # The message must name both the offending value and the label so the
    # failure is diagnosable from the log line alone
    assert repr(name) in str(exc_info.value)
    assert "db_schema" in str(exc_info.value)


@pytest.mark.parametrize(
    "path",
    [
        "cms_iom",
        "a",
        "_",
        "cms_iom.pub100_04.chapter12",
        "usc.title42",
        "a.b.c.d.e",
        "label_1.label_2",
        "1.2.3",
    ],
)
def test_validate_collection_path_valid_returned_unchanged(path: str) -> None:
    assert validate_collection_path(path) == path


def test_validate_collection_path_rejects_trailing_newline() -> None:
    # Same fullmatch-vs-match regression guard as the identifier validator
    with pytest.raises(ValueError, match="Invalid collection_path"):
        validate_collection_path("a.b\n")


@pytest.mark.parametrize(
    "path",
    [
        "CMS.iom",  # uppercase
        "a.B",  # uppercase label
        "a-b.c",  # dash
        "a b.c",  # space
        "qpp_cm.forms.aki-new-hd.pdf",  # authored .ext leaf on a dashed stem
        "cms_iom.ge101c01.PDF",  # authored .ext leaf with uppercase extension
        "a..b",  # doubled dot (empty label)
        ".a",  # leading dot
        "a.",  # trailing dot
        "",  # empty string
        "   ",  # whitespace-only
    ],
)
def test_validate_collection_path_invalid_raises_naming_path(path: str) -> None:
    with pytest.raises(ValueError, match="Invalid collection_path") as exc_info:
        validate_collection_path(path)
    assert repr(path) in str(exc_info.value)
