"""Unit tests for the structured-table create-or-append engine.

The DDL/DML paths run against a real ephemeral schema in ``ingestion_test`` (the
``ephemeral_schema`` fixture), because mocking DDL would be tautological. Each
test first creates a minimal parent ``sheet`` table and inserts a row per
``collection_path`` (the structured table's FK requires it). Tests skip cleanly
when no database is configured. Pure helpers (column mapping) need no database.
"""


import pytest
from ingpipe_excel_ingestion.excel_parser import ROW_NUMBER_KEY
from ingpipe_excel_ingestion.structured_table import build_column_mapping, write_rows
from ingpipe_lib.logconfig import setup_logging
from ingpipe_lib.paths import resolve_log_dir
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

setup_logging(
    log_dir=resolve_log_dir("ingpipe_excel_ingestion/unit_tests"),
    log_name="test_structured_table",
)


# ---------------------------------------------------------------------------
# build_column_mapping (no DB)
# ---------------------------------------------------------------------------


def test_build_column_mapping_normalizes_and_dedupes() -> None:
    """Headers map to deduplicated col_* names preserving order."""
    mapping = build_column_mapping(["CPT/HCPCS", "Question #", "Question"])
    assert mapping == {
        "CPT/HCPCS": "col_cpt_hcpcs",
        "Question #": "col_question",
        "Question": "col_question_2",
    }


def test_build_column_mapping_duplicate_raw_headers_raise() -> None:
    """Byte-identical raw headers raise instead of silently dropping a column.

    The mapping is keyed on the raw header, so ["A", "A"] used to collapse to
    a single entry — one column disappeared and the survivor took the last
    duplicate's values. For a direct caller that is data loss, not
    defense-in-depth, so the helper now refuses.
    """
    with pytest.raises(ValueError, match="Duplicate raw header"):
        build_column_mapping(["A", "A"])


# ---------------------------------------------------------------------------
# Helpers for DB tests
# ---------------------------------------------------------------------------


def _row(row_number: int, **values: str | None) -> dict[str, str | int | None]:
    """Build a parsed-row dict with a synthetic row_number."""
    return {ROW_NUMBER_KEY: row_number, **values}


def _parent(engine: Engine, schema: str, *collection_paths: str) -> None:
    """Create the parent ``sheet`` table and insert a row per collection_path.

    The structured table's FK references ``sheet(collection_path)``, so each
    collection_path used by a test must exist there first. This is a deliberate
    look-alike that carries only the FK-relevant column (``collection_path``);
    the production CHECK constraints (``n_rows``/``source_binary_hash``) are
    irrelevant to the structured-table FK/cascade behavior under test here.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                f'create table if not exists "{schema}".sheet ('
                "collection_path ltree primary key, "
                "title text not null, "
                "n_rows integer not null, "
                "source_binary_hash numeric(20,0) not null, "
                "structured_table text, "
                "ingested_at timestamptz not null default now())"
            )
        )
        for cp in collection_paths:
            conn.execute(
                text(
                    f'insert into "{schema}".sheet '
                    "(collection_path, title, n_rows, source_binary_hash) "
                    "values (:cp, :cp, 1, 0) "
                    "on conflict (collection_path) do nothing"
                ),
                {"cp": cp},
            )


def _fetch_all(engine: Engine, schema: str, table: str) -> list[dict[str, object]]:
    """Return all rows of a table as dicts, ordered by collection_path/sort_order."""
    with engine.connect() as conn:
        result = conn.execute(
            text(
                f'select * from "{schema}"."{table}" '
                "order by collection_path, sort_order"
            )
        )
        return [dict(r._mapping) for r in result]


def _columns(engine: Engine, schema: str, table: str) -> set[str]:
    """Return the column names of a table."""
    with engine.connect() as conn:
        result = conn.execute(
            text(
                "select column_name from information_schema.columns "
                "where table_schema = :s and table_name = :t"
            ),
            {"s": schema, "t": table},
        )
        return {r[0] for r in result}


def _table_comment(engine: Engine, schema: str, table: str) -> str | None:
    """Return a table's COMMENT ON description (obj_description), or None."""
    with engine.connect() as conn:
        return conn.execute(
            text(
                "select obj_description(c.oid, 'pg_class') "
                "from pg_class c "
                "inner join pg_namespace n on n.oid = c.relnamespace "
                "where n.nspname = :s and c.relname = :t"
            ),
            {"s": schema, "t": table},
        ).scalar_one()


# ---------------------------------------------------------------------------
# write_rows: create-on-first-write
# ---------------------------------------------------------------------------


def test_write_rows_creates_table_with_identity_and_data_cols(
    ephemeral_schema: tuple[Engine, str],
) -> None:
    """First write creates the table with identity + col_* columns and a PK."""
    engine, schema = ephemeral_schema
    _parent(engine, schema, "a.s")

    count = write_rows(
        engine, schema, "t1", "sheet", "a.s",
        column_names=["Code", "Label"],
        rows=[_row(1, Code="A1", Label="alpha"), _row(2, Code="B2", Label="beta")],
        overwrite=False, min_column_overlap=0.5,
    )

    assert count == 2
    cols = _columns(engine, schema, "t1")
    assert {"collection_path", "sort_order"} <= cols
    assert {"col_code", "col_label"} <= cols
    # ingested_at was dropped from the structured table (defer to sheet).
    assert "ingested_at" not in cols

    rows = _fetch_all(engine, schema, "t1")
    assert len(rows) == 2
    assert rows[0]["col_code"] == "A1"
    assert rows[0]["sort_order"] == 1
    assert rows[0]["collection_path"] == "a.s"


# ---------------------------------------------------------------------------
# write_rows: append paths
# ---------------------------------------------------------------------------


def test_write_rows_same_shape_append(
    ephemeral_schema: tuple[Engine, str],
) -> None:
    """A same-shape sheet from a second source appends, not duplicates."""
    engine, schema = ephemeral_schema
    _parent(engine, schema, "a.s", "b.s")
    common = dict(column_names=["Code"], overwrite=False, min_column_overlap=0.5)
    write_rows(engine, schema, "t", "sheet", "a.s", rows=[_row(1, Code="A")], **common)
    write_rows(engine, schema, "t", "sheet", "b.s", rows=[_row(1, Code="B")], **common)

    rows = _fetch_all(engine, schema, "t")
    assert {r["collection_path"] for r in rows} == {"a.s", "b.s"}
    assert len(rows) == 2


def test_write_rows_superset_append_adds_column(
    ephemeral_schema: tuple[Engine, str],
) -> None:
    """A sheet with extra columns triggers ADD COLUMN; older rows keep NULL."""
    engine, schema = ephemeral_schema
    _parent(engine, schema, "a.s", "b.s")
    write_rows(
        engine, schema, "t", "sheet", "a.s",
        column_names=["Code"], rows=[_row(1, Code="A")],
        overwrite=False, min_column_overlap=0.5,
    )
    write_rows(
        engine, schema, "t", "sheet", "b.s",
        column_names=["Code", "Extra"], rows=[_row(1, Code="B", Extra="x")],
        overwrite=False, min_column_overlap=0.5,
    )

    assert "col_extra" in _columns(engine, schema, "t")
    rows = _fetch_all(engine, schema, "t")
    by_path = {r["collection_path"]: r for r in rows}
    assert by_path["a.s"]["col_extra"] is None
    assert by_path["b.s"]["col_extra"] == "x"


def test_write_rows_subset_append_nulls_missing(
    ephemeral_schema: tuple[Engine, str],
) -> None:
    """A sheet missing a column inserts NULL for it (no error)."""
    engine, schema = ephemeral_schema
    _parent(engine, schema, "a.s", "b.s")
    write_rows(
        engine, schema, "t", "sheet", "a.s",
        column_names=["Code", "Label"], rows=[_row(1, Code="A", Label="alpha")],
        overwrite=False, min_column_overlap=0.5,
    )
    write_rows(
        engine, schema, "t", "sheet", "b.s",
        column_names=["Code"], rows=[_row(1, Code="B")],
        overwrite=False, min_column_overlap=0.5,
    )

    rows = _fetch_all(engine, schema, "t")
    by_path = {r["collection_path"]: r for r in rows}
    assert by_path["b.s"]["col_label"] is None


# ---------------------------------------------------------------------------
# write_rows: compatibility guard
# ---------------------------------------------------------------------------


def test_write_rows_guard_raises_on_disjoint_columns(
    ephemeral_schema: tuple[Engine, str],
) -> None:
    """A near-disjoint sheet raises (likely wrong-table clash)."""
    engine, schema = ephemeral_schema
    _parent(engine, schema, "a.s", "b.s")
    write_rows(
        engine, schema, "t", "sheet", "a.s",
        column_names=["Alpha", "Beta"], rows=[_row(1, Alpha="1", Beta="2")],
        overwrite=False, min_column_overlap=0.5,
    )
    with pytest.raises(ValueError, match="Incompatible columns"):
        write_rows(
            engine, schema, "t", "sheet", "b.s",
            column_names=["Gamma", "Delta"], rows=[_row(1, Gamma="3", Delta="4")],
            overwrite=False, min_column_overlap=0.5,
        )


def test_write_rows_guard_allows_high_overlap(
    ephemeral_schema: tuple[Engine, str],
) -> None:
    """A high-overlap sheet is allowed even with one differing column."""
    engine, schema = ephemeral_schema
    _parent(engine, schema, "a.s", "b.s")
    write_rows(
        engine, schema, "t", "sheet", "a.s",
        column_names=["Code", "Label"], rows=[_row(1, Code="A", Label="x")],
        overwrite=False, min_column_overlap=0.5,
    )
    # Shares col_code, col_label (2 of 3 incoming, 2 of 2 existing) -> allowed.
    count = write_rows(
        engine, schema, "t", "sheet", "b.s",
        column_names=["Code", "Label", "Note"],
        rows=[_row(1, Code="B", Label="y", Note="n")],
        overwrite=False, min_column_overlap=0.5,
    )
    assert count == 1


def test_write_rows_guard_allows_exact_threshold(
    ephemeral_schema: tuple[Engine, str],
) -> None:
    """Overlap exactly equal to the threshold is allowed (the guard is strict <)."""
    engine, schema = ephemeral_schema
    _parent(engine, schema, "a.s", "b.s")
    write_rows(
        engine, schema, "t", "sheet", "a.s",
        column_names=["Code", "Label"], rows=[_row(1, Code="A", Label="x")],
        overwrite=False, min_column_overlap=0.5,
    )
    # Shares col_code only: 1 of 2 incoming and 1 of 2 existing -> r_in=r_ex=0.5,
    # which EQUALS the threshold and must pass (a regression to `<=` would reject).
    count = write_rows(
        engine, schema, "t", "sheet", "b.s",
        column_names=["Code", "Other"], rows=[_row(1, Code="B", Other="z")],
        overwrite=False, min_column_overlap=0.5,
    )
    assert count == 1


# ---------------------------------------------------------------------------
# write_rows: overwrite / skip
# ---------------------------------------------------------------------------


def test_write_rows_overwrite_replaces_only_that_source(
    ephemeral_schema: tuple[Engine, str],
) -> None:
    """Overwrite deletes only the matching collection_path's rows before reinsert."""
    engine, schema = ephemeral_schema
    _parent(engine, schema, "a.s", "b.s")
    write_rows(
        engine, schema, "t", "sheet", "a.s",
        column_names=["Code"], rows=[_row(1, Code="A1"), _row(2, Code="A2")],
        overwrite=False, min_column_overlap=0.5,
    )
    write_rows(
        engine, schema, "t", "sheet", "b.s",
        column_names=["Code"], rows=[_row(1, Code="B1")],
        overwrite=False, min_column_overlap=0.5,
    )
    # Re-load a.s with overwrite -> a's rows replaced, b's untouched.
    write_rows(
        engine, schema, "t", "sheet", "a.s",
        column_names=["Code"], rows=[_row(1, Code="A-NEW")],
        overwrite=True, min_column_overlap=0.5,
    )

    rows = _fetch_all(engine, schema, "t")
    a_rows = [r for r in rows if r["collection_path"] == "a.s"]
    b_rows = [r for r in rows if r["collection_path"] == "b.s"]
    assert len(a_rows) == 1
    assert a_rows[0]["col_code"] == "A-NEW"
    assert len(b_rows) == 1
    assert b_rows[0]["col_code"] == "B1"


def test_write_rows_skip_if_present_when_not_overwrite(
    ephemeral_schema: tuple[Engine, str],
) -> None:
    """A second write of the same source with overwrite=false is skipped."""
    engine, schema = ephemeral_schema
    _parent(engine, schema, "a.s")
    write_rows(
        engine, schema, "t", "sheet", "a.s",
        column_names=["Code"], rows=[_row(1, Code="A")],
        overwrite=False, min_column_overlap=0.5,
    )
    marker = write_rows(
        engine, schema, "t", "sheet", "a.s",
        column_names=["Code"], rows=[_row(1, Code="A-CHANGED")],
        overwrite=False, min_column_overlap=0.5,
    )

    assert marker == -1
    rows = _fetch_all(engine, schema, "t")
    assert len(rows) == 1
    assert rows[0]["col_code"] == "A"  # unchanged


# ---------------------------------------------------------------------------
# write_rows: identifier validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("position", ["db_schema", "table_name", "sheet_table"])
def test_write_rows_rejects_unsafe_identifier(
    ephemeral_schema: tuple[Engine, str], position: str
) -> None:
    """A hostile identifier in any position raises before any SQL is generated.

    ``write_rows`` is the entry point that validates the three config-supplied
    identifiers it splices into DDL, so each position is pinned here (not only
    at the validator one module away): dropping any of the three
    ``validate_sql_identifier`` calls must fail this test.
    """
    engine, schema = ephemeral_schema
    names = {"db_schema": schema, "table_name": "t", "sheet_table": "sheet"}
    names[position] = 'bad"; drop table t --'

    with pytest.raises(ValueError, match=f"Unsafe SQL identifier for {position}"):
        write_rows(
            engine, names["db_schema"], names["table_name"], names["sheet_table"],
            "a.s",
            column_names=["Code"], rows=[_row(1, Code="A")],
            overwrite=False, min_column_overlap=0.5,
        )


# ---------------------------------------------------------------------------
# write_rows: FK cascade from the parent sheet
# ---------------------------------------------------------------------------


def test_deleting_parent_sheet_cascades_structured_rows(
    ephemeral_schema: tuple[Engine, str],
) -> None:
    """Deleting a sheet's parent row cascades its structured rows away."""
    engine, schema = ephemeral_schema
    _parent(engine, schema, "a.s")
    write_rows(
        engine, schema, "t", "sheet", "a.s",
        column_names=["Code"], rows=[_row(1, Code="A"), _row(2, Code="B")],
        overwrite=False, min_column_overlap=0.5,
    )
    assert len(_fetch_all(engine, schema, "t")) == 2

    with engine.begin() as conn:
        conn.execute(
            text(f'delete from "{schema}".sheet where collection_path = :cp'),
            {"cp": "a.s"},
        )

    assert _fetch_all(engine, schema, "t") == []


# ---------------------------------------------------------------------------
# write_rows: table comments
# ---------------------------------------------------------------------------


def test_write_rows_applies_table_comment_with_quote_safety(
    ephemeral_schema: tuple[Engine, str],
) -> None:
    """A set table_comment lands as the table's obj_description, quotes intact.

    The text carries a single quote (plus & and an em dash) to pin that the
    comment is bound as data, not spliced into the SQL literal.
    """
    engine, schema = ephemeral_schema
    _parent(engine, schema, "a.s")
    comment = "Trigger code list — it's the measure's E&M episode triggers"

    write_rows(
        engine, schema, "t", "sheet", "a.s",
        column_names=["Code"], rows=[_row(1, Code="A")],
        overwrite=False, min_column_overlap=0.5,
        table_comment=comment,
    )

    assert _table_comment(engine, schema, "t") == comment


def test_write_rows_without_table_comment_issues_none(
    ephemeral_schema: tuple[Engine, str],
) -> None:
    """With table_comment unset, no COMMENT ON is issued (description stays NULL)."""
    engine, schema = ephemeral_schema
    _parent(engine, schema, "a.s")

    write_rows(
        engine, schema, "t", "sheet", "a.s",
        column_names=["Code"], rows=[_row(1, Code="A")],
        overwrite=False, min_column_overlap=0.5,
    )

    assert _table_comment(engine, schema, "t") is None


def test_write_rows_rerun_refreshes_table_comment(
    ephemeral_schema: tuple[Engine, str],
) -> None:
    """The comment is applied on EVERY write (not create-only): a re-run —
    even one skipped as already-present — refreshes the text."""
    engine, schema = ephemeral_schema
    _parent(engine, schema, "a.s")
    common = dict(
        column_names=["Code"], rows=[_row(1, Code="A")],
        overwrite=False, min_column_overlap=0.5,
    )

    write_rows(
        engine, schema, "t", "sheet", "a.s", table_comment="old text", **common
    )
    # Second write is a data skip (-1) but must still refresh the comment.
    marker = write_rows(
        engine, schema, "t", "sheet", "a.s", table_comment="new text", **common
    )

    assert marker == -1
    assert _table_comment(engine, schema, "t") == "new text"


# ---------------------------------------------------------------------------
# write_rows: edge cases (empty rows, composite PK)
# ---------------------------------------------------------------------------


def test_write_rows_empty_rows_returns_zero(
    ephemeral_schema: tuple[Engine, str],
) -> None:
    """An empty row list creates the table, inserts nothing, and returns 0."""
    engine, schema = ephemeral_schema
    _parent(engine, schema, "a.s")
    count = write_rows(
        engine, schema, "t", "sheet", "a.s",
        column_names=["Code"], rows=[],
        overwrite=False, min_column_overlap=0.5,
    )
    assert count == 0
    assert _fetch_all(engine, schema, "t") == []


def test_write_rows_composite_pk_rejects_duplicate(
    ephemeral_schema: tuple[Engine, str],
) -> None:
    """The composite PK (collection_path, sort_order) rejects a duplicate row."""
    engine, schema = ephemeral_schema
    _parent(engine, schema, "a.s")
    write_rows(
        engine, schema, "t", "sheet", "a.s",
        column_names=["Code"], rows=[_row(1, Code="A")],
        overwrite=False, min_column_overlap=0.5,
    )
    # Re-inserting the same (collection_path, sort_order) must violate the PK.
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    f'insert into "{schema}".t (collection_path, sort_order) '
                    "values (:cp, 1)"
                ),
                {"cp": "a.s"},
            )
