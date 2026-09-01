"""Real-database tests for generate_embeddings against an ephemeral schema.

These replace the old string-asserted SELECT/INSERT/DELETE tests: the
production SQL (embedding-table DDL with its HNSW/GIN indexes, the ``::vector``
cast, the left-anti-join resume SELECT, the scoped overwrite DELETE, and the
per-batch transaction) is executed by PostgreSQL and asserted on resulting
table state. A stub SentenceTransformer keeps the suite fast — only the model
is stubbed; every statement is real.

Tests skip cleanly when the ``ingestion_test`` database is unreachable.
"""

from collections.abc import Iterator

import numpy as np
import pytest
from ingpipe_embedding_generation.generate_embeddings import generate_embeddings
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

EMBED_DIM = 4


class _StubTokenizer:
    """Deterministic word-based tokenizer: one token per word plus 2 specials."""

    def __call__(self, text_value: str, truncation: bool = False) -> dict[str, list[int]]:
        return {"input_ids": list(range(len(text_value.split()) + 2))}


class _StubModel:
    """Stub SentenceTransformer: fixed dimension, zero vectors, word tokenizer."""

    def __init__(self) -> None:
        self.tokenizer = _StubTokenizer()

    def get_sentence_embedding_dimension(self) -> int:
        return EMBED_DIM

    def encode(self, texts: list[str], **_kwargs: object) -> np.ndarray:
        return np.zeros((len(texts), EMBED_DIM), dtype=float)


@pytest.fixture
def stub_model(mocker) -> Iterator[_StubModel]:
    """Patch the model loader with the stub so no real model is loaded."""
    model = _StubModel()
    mocker.patch(
        "ingpipe_embedding_generation.generate_embeddings._get_embedding_model",
        return_value=model,
    )
    yield model


def _create_source_table(engine: Engine, schema: str, rows: list[tuple[str, int, str | None, str | None]]) -> None:
    """Create a source table with a composite PK and insert the given rows.

    Columns: (filename text, sort_order int) PK + heading_text, content_text.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                f"create table {schema}.src ("
                "filename text not null, sort_order integer not null, "
                "heading_text text, content_text text, "
                "primary key (filename, sort_order))"
            )
        )
        for filename, sort_order, heading, content in rows:
            conn.execute(
                text(
                    f"insert into {schema}.src "
                    "(filename, sort_order, heading_text, content_text) "
                    "values (:f, :s, :h, :c)"
                ),
                {"f": filename, "s": sort_order, "h": heading, "c": content},
            )


def _run(schema: str, *, overwrite: bool = False, source_filter: dict | None = None,
         max_tokens: int = 500, overlap_tokens: int = 5,
         header_columns: list[str] | None = None) -> int:
    """Invoke generate_embeddings against the ephemeral schema's src table."""
    return generate_embeddings(
        db_name="ingestion_test",
        db_schema=schema,
        source_table="src",
        embedding_table="src_embedding",
        embed_columns=["content_text"],
        model_name="stub-model",
        batch_size=8,
        max_tokens=max_tokens,
        overlap_tokens=overlap_tokens,
        overwrite=overwrite,
        source_filter=source_filter,
        header_columns=header_columns,
    )


def _embedding_rows(engine: Engine, schema: str) -> list[dict]:
    """Fetch all embedding rows ordered by PK + chunk_number."""
    with engine.connect() as conn:
        result = conn.execute(
            text(
                f"select filename, sort_order, chunk_number, chunk_text, "
                f"word_count, vector_dims(embedding) as dims "
                f"from {schema}.src_embedding "
                "order by filename, sort_order, chunk_number"
            )
        )
        return [dict(r._mapping) for r in result]


class TestGenerateEmbeddingsRealDatabase:
    """End-to-end generate_embeddings runs against real PostgreSQL."""

    def test_end_to_end_inserts_vectors_and_resumes(
        self, ephemeral_schema: tuple[Engine, str], stub_model: _StubModel
    ) -> None:
        """Rows embed with real vectors; a re-run resumes via the anti-join.

        Proves the DDL executes (vector column, indexes), the ``::vector``
        cast stores a queryable vector of the model's dimension, and the
        left-anti-join resume selects only rows without embeddings.
        """
        engine, schema = ephemeral_schema
        _create_source_table(
            engine, schema,
            [("a.pdf", 1, "Heading A", "alpha body"),
             ("a.pdf", 2, "Heading B", "beta body")],
        )

        inserted = _run(schema)

        assert inserted == 2
        rows = _embedding_rows(engine, schema)
        assert [(r["filename"], r["sort_order"]) for r in rows] == [
            ("a.pdf", 1), ("a.pdf", 2)
        ]
        # The stored embedding is a real pgvector value of the stub dimension.
        assert all(r["dims"] == EMBED_DIM for r in rows)
        # The embed text is the "col: value" rendering of embed_columns.
        assert rows[0]["chunk_text"] == "content_text: alpha body"
        # The chunk_tsv generated column + its GIN index and the HNSW index
        # were created by the real DDL.
        with engine.connect() as conn:
            tsv = conn.execute(
                text(
                    "select count(*) from information_schema.columns "
                    "where table_schema = :s and table_name = 'src_embedding' "
                    "and column_name = 'chunk_tsv' and is_generated = 'ALWAYS'"
                ),
                {"s": schema},
            ).scalar_one()
            indexes = conn.execute(
                text(
                    "select indexdef from pg_indexes "
                    "where schemaname = :s and tablename = 'src_embedding'"
                ),
                {"s": schema},
            ).fetchall()
        assert tsv == 1
        assert any("hnsw" in row[0] for row in indexes)
        assert any("gin" in row[0] for row in indexes)

        # Resume: nothing left to embed.
        assert _run(schema) == 0
        # A new source row is picked up by the anti-join; existing rows are not
        # re-embedded (still exactly one chunk each).
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"insert into {schema}.src values ('b.pdf', 1, 'H', 'gamma body')"
                )
            )
        assert _run(schema) == 1
        assert len(_embedding_rows(engine, schema)) == 3

    def test_long_text_chunks_and_batches_commit(
        self, ephemeral_schema: tuple[Engine, str], stub_model: _StubModel
    ) -> None:
        """A long row splits into multiple ordered chunks, all committed."""
        engine, schema = ephemeral_schema
        long_text = " ".join(f"word{i}" for i in range(30))
        _create_source_table(engine, schema, [("a.pdf", 1, "H", long_text)])

        inserted = _run(schema, max_tokens=12, overlap_tokens=2)

        rows = _embedding_rows(engine, schema)
        assert inserted == len(rows) > 1
        assert [r["chunk_number"] for r in rows] == list(range(1, len(rows) + 1))

    def test_overwrite_deletes_existing_and_regenerates(
        self, ephemeral_schema: tuple[Engine, str], stub_model: _StubModel
    ) -> None:
        """overwrite=True really DELETEs matching embeddings before re-inserting.

        A stale bogus chunk row is planted for an embedded source row; without
        overwrite the anti-join treats the row as done (bogus row survives);
        with overwrite the DELETE removes it and the row is regenerated clean.
        """
        engine, schema = ephemeral_schema
        _create_source_table(engine, schema, [("a.pdf", 1, "H", "alpha body")])
        _run(schema)
        # Plant a stale extra chunk for the already-embedded row.
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"insert into {schema}.src_embedding "
                    "(filename, sort_order, chunk_number, chunk_text, word_count, embedding) "
                    "values ('a.pdf', 1, 99, 'stale', 1, "
                    f"cast(:v as vector))"
                ),
                {"v": "[" + ",".join(["0"] * EMBED_DIM) + "]"},
            )

        # Resume run: row already embedded, bogus row survives untouched.
        assert _run(schema, overwrite=False) == 0
        assert len(_embedding_rows(engine, schema)) == 2

        # Overwrite run: DELETE clears both, regeneration stores exactly one.
        assert _run(schema, overwrite=True) == 1
        rows = _embedding_rows(engine, schema)
        assert [r["chunk_number"] for r in rows] == [1]
        assert rows[0]["chunk_text"] == "content_text: alpha body"

    def test_source_filter_scopes_select_and_overwrite_delete(
        self, ephemeral_schema: tuple[Engine, str], stub_model: _StubModel
    ) -> None:
        """source_filter LIKE patterns scope both the SELECT and the DELETE."""
        engine, schema = ephemeral_schema
        _create_source_table(
            engine, schema,
            [("keep.pdf", 1, "H", "kept body"),
             ("skip.pdf", 1, "H", "skipped body")],
        )

        # Only the matching row embeds.
        assert _run(schema, source_filter={"filename": "keep%"}) == 1
        rows = _embedding_rows(engine, schema)
        assert [r["filename"] for r in rows] == ["keep.pdf"]

        # Embed the rest, then overwrite with the filter: only the matching
        # row's embedding is deleted and regenerated (the other survives).
        assert _run(schema) == 1
        assert _run(schema, overwrite=True, source_filter={"filename": "keep%"}) == 1
        rows = _embedding_rows(engine, schema)
        assert [r["filename"] for r in rows] == ["keep.pdf", "skip.pdf"]

    def test_header_columns_prefix_every_chunk(
        self, ephemeral_schema: tuple[Engine, str], stub_model: _StubModel
    ) -> None:
        """header_columns prepend the header text to every stored chunk."""
        engine, schema = ephemeral_schema
        long_text = " ".join(f"word{i}" for i in range(30))
        _create_source_table(engine, schema, [("a.pdf", 1, "Chapter 1", long_text)])

        _run(schema, max_tokens=15, overlap_tokens=2, header_columns=["heading_text"])

        rows = _embedding_rows(engine, schema)
        assert len(rows) > 1
        assert all(
            r["chunk_text"].startswith("heading_text: Chapter 1\n") for r in rows
        )

    def test_failed_batch_commits_nothing(
        self, ephemeral_schema: tuple[Engine, str], stub_model: _StubModel
    ) -> None:
        """A batch failing mid-executemany rolls back the whole batch.

        The embedding table is sabotaged (chunk_number 2 violates a CHECK), so
        a multi-chunk row's insert fails after chunk 1 was already executed in
        the same transaction — nothing of the batch may be committed.
        """
        engine, schema = ephemeral_schema
        long_text = " ".join(f"word{i}" for i in range(30))
        _create_source_table(engine, schema, [("a.pdf", 1, "H", long_text)])
        # First run against an empty filter creates the table without
        # inserting: use a filter matching nothing.
        assert _run(schema, source_filter={"filename": "nomatch%"}) == 0
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"alter table {schema}.src_embedding "
                    "add constraint fail_second check (chunk_number <> 2)"
                )
            )

        with pytest.raises(SQLAlchemyError):
            _run(schema, max_tokens=12, overlap_tokens=2)

        # The failed executemany's transaction rolled back whole: chunk 1 was
        # executed before chunk 2 failed, yet nothing is committed.
        assert _embedding_rows(engine, schema) == []
