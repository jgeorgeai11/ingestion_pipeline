"""Unit tests for ingpipe_lib.ddl (the shared DDL template renderer)."""

from pathlib import Path

import pytest
from ingpipe_lib.ddl import render_ddl_template


def test_substitutes_every_placeholder(tmp_path: Path) -> None:
    """Each {placeholder} is replaced by its validated identifier."""
    template = tmp_path / "schema.sql"
    template.write_text(
        "create schema if not exists {schema_name};\n"
        "create table {schema_name}.{doc_table} (id int);\n",
        encoding="utf-8",
    )

    rendered = render_ddl_template(
        template, {"schema_name": "cms_iom", "doc_table": "document"}
    )

    assert "create schema if not exists cms_iom;" in rendered
    assert "create table cms_iom.document (id int);" in rendered
    assert "{" not in rendered


def test_missing_template_raises_file_not_found(tmp_path: Path) -> None:
    """A missing template path raises FileNotFoundError naming it."""
    missing = tmp_path / "absent.sql"
    with pytest.raises(FileNotFoundError, match=r"absent\.sql"):
        render_ddl_template(missing, {})


def test_leftover_placeholder_raises_value_error(tmp_path: Path) -> None:
    """A placeholder the caller does not substitute fails loudly.

    Template/code drift guard: the surviving {...} would otherwise fail later
    with an obscure DDL execution error.
    """
    template = tmp_path / "schema.sql"
    template.write_text(
        "create table {schema_name}.{new_table} (id int);\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match=r"new_table"):
        render_ddl_template(template, {"schema_name": "cms_iom"})


def test_reads_template_as_utf8(tmp_path: Path) -> None:
    """Non-ASCII comment text (em dashes) survives the read intact."""
    template = tmp_path / "schema.sql"
    template.write_text(
        "comment on table {t} is 'Catalog — one row per sheet';\n",
        encoding="utf-8",
    )

    rendered = render_ddl_template(template, {"t": "sheet"})

    assert "Catalog — one row per sheet" in rendered
