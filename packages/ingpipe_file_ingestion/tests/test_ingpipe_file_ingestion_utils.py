"""Tests for shared ingpipe-file-ingestion utilities."""

from pathlib import Path
from unittest.mock import MagicMock

import ingpipe_file_ingestion
import pytest
from ingpipe_file_ingestion._utils import (
    ensure_schema,
    validate_collection_path,
    validate_sql_identifier,
)
from pytest_mock import MockerFixture
from sqlalchemy.exc import SQLAlchemyError


class TestValidateCollectionPath:
    """Tests for collection_path ltree validation."""

    def test_validate_collection_path_multi_label_unchanged(self) -> None:
        """A valid lowercase ltree of several labels passes through untouched."""
        assert (
            validate_collection_path("cms_iom.pub_100_01.ge101c01")
            == "cms_iom.pub_100_01.ge101c01"
        )

    def test_validate_collection_path_single_label_unchanged(self) -> None:
        """A single valid label passes through untouched."""
        assert validate_collection_path("usc05") == "usc05"

    def test_validate_collection_path_digits_and_underscores_unchanged(self) -> None:
        """Labels of digits and underscores are valid and unchanged."""
        assert (
            validate_collection_path("qpp_cm.2025_cost_measure.2025_12_py2025")
            == "qpp_cm.2025_cost_measure.2025_12_py2025"
        )

    def test_validate_collection_path_uppercase_raises(self) -> None:
        """An uppercase character makes the path invalid."""
        with pytest.raises(ValueError, match="Invalid collection_path"):
            validate_collection_path("cms_iom.Pub_100_01.ge101c01")

    def test_validate_collection_path_dash_raises(self) -> None:
        """A dash is not a permitted ltree character."""
        with pytest.raises(ValueError, match="Invalid collection_path"):
            validate_collection_path("qpp_cm.2025-cost-measure.aki-new-hd")

    def test_validate_collection_path_space_raises(self) -> None:
        """A space is not a permitted ltree character."""
        with pytest.raises(ValueError, match="Invalid collection_path"):
            validate_collection_path("data.attachment 1.sow")

    def test_validate_collection_path_dashed_extension_leaf_raises(self) -> None:
        """An authored leaf like ``aki-new-hd.pdf`` is invalid (the dashes break it)."""
        # The old sanitizer would strip ``.pdf`` and fold the dashes; the
        # validator instead rejects the whole path so the caller skips it.
        with pytest.raises(ValueError, match="Invalid collection_path"):
            validate_collection_path("qpp_cm.forms.aki-new-hd.pdf")

    def test_validate_collection_path_uppercase_extension_leaf_raises(self) -> None:
        """A leaf carrying an uppercase ``.PDF`` extension is invalid."""
        with pytest.raises(ValueError, match="Invalid collection_path"):
            validate_collection_path("cms_iom.pub_100_01.ge101c01.PDF")

    def test_validate_collection_path_leading_dot_raises(self) -> None:
        """A leading dot creates an empty first label and is invalid."""
        with pytest.raises(ValueError, match="Invalid collection_path"):
            validate_collection_path(".cms_iom.pub_100_01")

    def test_validate_collection_path_trailing_dot_raises(self) -> None:
        """A trailing dot creates an empty last label and is invalid."""
        with pytest.raises(ValueError, match="Invalid collection_path"):
            validate_collection_path("cms_iom.pub_100_01.")

    def test_validate_collection_path_double_dot_raises(self) -> None:
        """A doubled dot creates an empty interior label and is invalid."""
        with pytest.raises(ValueError, match="Invalid collection_path"):
            validate_collection_path("cms_iom..ge101c01")

    def test_validate_collection_path_empty_string_raises(self) -> None:
        """An empty input is invalid."""
        with pytest.raises(ValueError, match="Invalid collection_path"):
            validate_collection_path("")

    def test_validate_collection_path_blank_string_raises(self) -> None:
        """A whitespace-only input is invalid."""
        with pytest.raises(ValueError, match="Invalid collection_path"):
            validate_collection_path("   ")

    def test_validate_collection_path_trailing_newline_raises(self) -> None:
        """A trailing newline must be rejected (fullmatch, not match)."""
        with pytest.raises(ValueError, match="Invalid collection_path"):
            validate_collection_path("a.b\n")


class TestValidateSqlIdentifier:
    """Tests for safe SQL identifier validation."""

    @pytest.mark.parametrize("ident", ["document", "_private", "tbl_2025", "x"])
    def test_validate_sql_identifier_valid_returns_unchanged(self, ident: str) -> None:
        """A safe identifier passes through untouched."""
        assert validate_sql_identifier(ident, "label") == ident

    @pytest.mark.parametrize(
        "ident",
        ["Document", "2025_tbl", "my-table", "my table", "a.b", ""],
    )
    def test_validate_sql_identifier_unsafe_raises(self, ident: str) -> None:
        """An identifier with unsafe characters is rejected."""
        with pytest.raises(ValueError, match="Unsafe SQL identifier"):
            validate_sql_identifier(ident, "db_schema")

    def test_validate_sql_identifier_label_appears_in_error_message(self) -> None:
        """The diagnostic label is interpolated into the error message."""
        with pytest.raises(ValueError, match="db_schema"):
            validate_sql_identifier("Bad", "db_schema")

    def test_validate_sql_identifier_trailing_newline_raises(self) -> None:
        """A trailing newline must be rejected (fullmatch, not match)."""
        with pytest.raises(ValueError, match="Unsafe SQL identifier"):
            validate_sql_identifier("public\n", "db_schema")


def _make_mock_engine() -> tuple[MagicMock, MagicMock]:
    """Build a mock Engine whose ``begin()`` is a context manager yielding a conn.

    Returns:
        A tuple of (engine, conn) MagicMocks. ``conn`` is what ``with
        engine.begin() as conn`` binds to.
    """
    engine = MagicMock()
    conn = engine.begin.return_value.__enter__.return_value
    return engine, conn


class TestEnsureSchema:
    """Tests for the schema-bootstrap helper."""

    def test_ensure_schema_renders_all_placeholders(self, tmp_path: Path) -> None:
        """All three placeholders are substituted into the executed DDL."""
        ddl = tmp_path / "schema.sql"
        ddl.write_text(
            "CREATE SCHEMA {schema_name}; "
            "CREATE TABLE {schema_name}.{document_table} (); "
            "CREATE TABLE {schema_name}.{content_table} ();",
            encoding="utf-8",
        )
        engine, conn = _make_mock_engine()

        ensure_schema(engine, "rag", ddl, "doc", "doc_content")

        executed = str(conn.execute.call_args[0][0])
        assert "rag" in executed
        assert "doc" in executed
        assert "doc_content" in executed
        assert "{schema_name}" not in executed
        assert "{document_table}" not in executed
        assert "{content_table}" not in executed

    def test_ensure_schema_executes_ddl_in_transaction(self, tmp_path: Path) -> None:
        """The rendered DDL runs exactly once inside engine.begin()."""
        ddl = tmp_path / "schema.sql"
        ddl.write_text("CREATE SCHEMA {schema_name};", encoding="utf-8")
        engine, conn = _make_mock_engine()

        ensure_schema(engine, "rag", ddl, "doc", "doc_content")

        engine.begin.assert_called_once()
        conn.execute.assert_called_once()

    def test_ensure_schema_defaults_land_in_rendered_sql(self, tmp_path: Path) -> None:
        """Omitting the table args substitutes the documented defaults."""
        ddl = tmp_path / "schema.sql"
        ddl.write_text(
            "CREATE TABLE {schema_name}.{document_table} (); "
            "CREATE TABLE {schema_name}.{content_table} ();",
            encoding="utf-8",
        )
        engine, conn = _make_mock_engine()

        ensure_schema(engine, "rag", ddl)

        executed = str(conn.execute.call_args[0][0])
        assert "document" in executed
        assert "document_content" in executed

    def test_ensure_schema_stray_placeholder_raises(self, tmp_path: Path) -> None:
        """An unsubstituted placeholder in the template fails loudly."""
        ddl = tmp_path / "schema.sql"
        ddl.write_text(
            "CREATE SCHEMA {schema_name}; "
            "CREATE TABLE {schema_name}.{document_table} (); "
            "CREATE TABLE {schema_name}.{content_table} (); "
            "CREATE INDEX ON {bogus};",
            encoding="utf-8",
        )
        engine, _ = _make_mock_engine()

        with pytest.raises(ValueError, match=r"\{bogus\}"):
            ensure_schema(engine, "rag", ddl, "doc", "doc_content")
        engine.begin.assert_not_called()

    def test_ensure_schema_missing_ddl_raises(self, tmp_path: Path) -> None:
        """A nonexistent DDL path raises FileNotFoundError before any DB call."""
        engine, _ = _make_mock_engine()

        with pytest.raises(FileNotFoundError, match="DDL template not found"):
            ensure_schema(engine, "rag", tmp_path / "missing.sql")
        engine.begin.assert_not_called()

    @pytest.mark.parametrize(
        ("db_schema", "document_table", "content_table"),
        [
            ("bad-schema", "doc", "doc_content"),
            ("rag", "Bad-Doc", "doc_content"),
            ("rag", "doc", "bad content"),
        ],
    )
    def test_ensure_schema_unsafe_identifier_raises_before_execution(
        self,
        tmp_path: Path,
        db_schema: str,
        document_table: str,
        content_table: str,
    ) -> None:
        """Each of the three identifier args is rejected before any DB call.

        Parametrized over all three so dropping (or double-applying) one
        validation call cannot pass unnoticed: a config-supplied table name is
        interpolated into the DDL, so it is an injection surface too.
        """
        ddl = tmp_path / "schema.sql"
        ddl.write_text("CREATE SCHEMA {schema_name};", encoding="utf-8")
        engine, _ = _make_mock_engine()

        with pytest.raises(ValueError, match="Unsafe SQL identifier"):
            ensure_schema(engine, db_schema, ddl, document_table, content_table)
        engine.begin.assert_not_called()

    def test_ensure_schema_read_failure_propagates(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """An OSError from reading the DDL propagates before any DB call."""
        ddl = tmp_path / "schema.sql"
        ddl.write_text("CREATE SCHEMA {schema_name};", encoding="utf-8")
        mocker.patch.object(Path, "read_text", side_effect=OSError("boom"))
        engine, _ = _make_mock_engine()

        with pytest.raises(OSError, match="boom"):
            ensure_schema(engine, "rag", ddl)
        engine.begin.assert_not_called()

    def test_ensure_schema_sqlalchemy_error_propagates(self, tmp_path: Path) -> None:
        """A SQLAlchemyError from conn.execute is re-raised."""
        ddl = tmp_path / "schema.sql"
        ddl.write_text("CREATE SCHEMA {schema_name};", encoding="utf-8")
        engine, conn = _make_mock_engine()
        conn.execute.side_effect = SQLAlchemyError("boom")

        with pytest.raises(SQLAlchemyError):
            ensure_schema(engine, "rag", ddl)


class TestEnsureSchemaComments:
    """Tests for the baked template comments and the config comment overrides."""

    # The real shipped template, not a tmp file: pins that the template itself
    # carries the generic COMMENT ON TABLE statements for both tables.
    REAL_TEMPLATE = Path(ingpipe_file_ingestion.__file__).resolve().parent / "sql" / "schema.sql"

    def test_ensure_schema_real_template_renders_table_comments(self) -> None:
        """The rendered production template comments both tables, placeholders substituted."""
        engine, conn = _make_mock_engine()

        ensure_schema(engine, "rag", self.REAL_TEMPLATE, "doc", "doc_content")

        executed = str(conn.execute.call_args_list[0][0][0])
        assert "comment on table rag.doc is" in executed
        assert "comment on table rag.doc_content is" in executed
        assert "{" not in executed
        # The content-table comment's em dash pins the explicit encoding="utf-8"
        # read in ensure_schema: on a cp1252 default the read would either fail
        # or mojibake this character, and nothing else in the suite would notice.
        assert "—" in executed

    def test_ensure_schema_all_overrides_issue_comment_statements_with_bound_text(
        self, tmp_path: Path
    ) -> None:
        """Each override issues its COMMENT ON with the text as a bound parameter.

        The single quote in the document text must survive intact in the bound
        parameter (the driver quotes it; no hand-rolled literal escaping).
        """
        ddl = tmp_path / "schema.sql"
        ddl.write_text(
            "CREATE TABLE {schema_name}.{document_table} ({content_table} text);",
            encoding="utf-8",
        )
        engine, conn = _make_mock_engine()

        ensure_schema(
            engine, "rag", ddl, "doc", "doc_content",
            schema_comment="QPP cost-measure data — codes & lookups",
            document_table_comment="It's the MIF document table",
            content_table_comment="Sections / rows",
        )

        calls = conn.execute.call_args_list
        # 1 DDL execution + 3 comment statements, all in one transaction.
        engine.begin.assert_called_once()
        assert len(calls) == 4
        statements = [(str(c[0][0]), c[0][1]) for c in calls[1:]]
        assert statements[0] == (
            "comment on schema rag is :comment_text",
            {"comment_text": "QPP cost-measure data — codes & lookups"},
        )
        assert statements[1] == (
            "comment on table rag.doc is :comment_text",
            {"comment_text": "It's the MIF document table"},
        )
        assert statements[2] == (
            "comment on table rag.doc_content is :comment_text",
            {"comment_text": "Sections / rows"},
        )

    @pytest.mark.parametrize(
        ("kwargs", "expected_statement"),
        [
            (
                {"schema_comment": "Schema text"},
                "comment on schema rag is :comment_text",
            ),
            (
                {"document_table_comment": "Doc text"},
                "comment on table rag.doc is :comment_text",
            ),
            (
                {"content_table_comment": "Content text"},
                "comment on table rag.doc_content is :comment_text",
            ),
        ],
    )
    def test_ensure_schema_single_override_issues_only_that_comment(
        self, tmp_path: Path, kwargs: dict[str, str], expected_statement: str
    ) -> None:
        """A lone override issues exactly one COMMENT ON, the matching one."""
        ddl = tmp_path / "schema.sql"
        ddl.write_text(
            "CREATE TABLE {schema_name}.{document_table} ({content_table} text);",
            encoding="utf-8",
        )
        engine, conn = _make_mock_engine()

        ensure_schema(engine, "rag", ddl, "doc", "doc_content", **kwargs)

        calls = conn.execute.call_args_list
        assert len(calls) == 2  # DDL + the single comment
        assert str(calls[1][0][0]) == expected_statement

    def test_ensure_schema_no_overrides_issue_no_comment_statements(
        self, tmp_path: Path
    ) -> None:
        """With every override None (the default), only the DDL is executed."""
        ddl = tmp_path / "schema.sql"
        ddl.write_text(
            "CREATE TABLE {schema_name}.{document_table} ({content_table} text);",
            encoding="utf-8",
        )
        engine, conn = _make_mock_engine()

        ensure_schema(engine, "rag", ddl, "doc", "doc_content")

        conn.execute.assert_called_once()
        assert "comment on" not in str(conn.execute.call_args[0][0]).lower()
