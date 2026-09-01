"""Unit tests for chunker.py.

Tests the chunk_long_sections function with dict-based inputs and an injected
deterministic ``count_tokens`` (no real model is loaded). Verifies passthrough
for short sections, token-budgeted splitting for long ones, overlap behavior,
word-boundary preservation, and the validation errors.
"""

from collections.abc import Callable

import pytest
from ingpipe_embedding_generation.chunker import chunk_long_sections


def whitespace_token_counter(special_tokens: int = 2) -> Callable[[str], int]:
    """Build a deterministic word-based token counter for tests.

    Counts one token per whitespace-delimited word plus a fixed number of
    special tokens, mirroring how a real tokenizer adds (e.g.) a [CLS]/[SEP]
    pair. This keeps token budgets exactly predictable without loading a model.

    Args:
        special_tokens: Constant added to every count (model special tokens).

    Returns:
        A callable mapping a string to its token count.
    """

    def count_tokens(text: str) -> int:
        return len(text.split()) + special_tokens

    return count_tokens


class TestChunkLongSections:
    """Tests for chunk_long_sections."""

    def test_short_section_passthrough(self) -> None:
        """Section under the token budget passes through with chunk_number=1."""
        sections = [{"content_text": "hello world foo bar"}]
        # 4 words + 2 special = 6 tokens <= 10
        results = chunk_long_sections(
            sections, max_tokens=10, overlap_tokens=2, count_tokens=whitespace_token_counter()
        )

        assert len(results) == 1
        chunk, chunk_number = results[0]
        assert chunk_number == 1
        assert chunk["content_text"] == "hello world foo bar"
        assert chunk["word_count"] == 4

    def test_exact_max_tokens_passthrough(self) -> None:
        """Section with exactly max_tokens tokens passes through without splitting."""
        text = " ".join(f"word{i}" for i in range(8))
        sections = [{"content_text": text}]
        # 8 words + 2 special = 10 tokens == max_tokens
        results = chunk_long_sections(
            sections, max_tokens=10, overlap_tokens=2, count_tokens=whitespace_token_counter()
        )

        assert len(results) == 1
        assert results[0][1] == 1
        assert results[0][0]["word_count"] == 8

    def test_whitespace_only_oversize_section_skipped_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A wordless section over the budget yields no chunks and warns.

        Reproduces the silent-data-loss defect: a section of bare newlines
        tokenizes over the budget but splits to zero words. It must be skipped
        with a visible WARNING rather than silently vanishing.
        """
        # Every character counts as a token here, so the section is far over
        # the budget while .split() yields no words.
        def char_counter(text: str) -> int:
            return len(text)

        sections = [{"content_text": "\n" * 100}]

        with caplog.at_level("WARNING"):
            results = chunk_long_sections(
                sections, max_tokens=10, overlap_tokens=2, count_tokens=char_counter
            )

        assert results == []
        assert any(
            "no whitespace-delimited words" in record.message
            for record in caplog.records
        )

    def test_empty_string_section_still_passthrough(self) -> None:
        """An empty-string section under the budget takes the passthrough path."""
        sections = [{"content_text": ""}]
        # 0 words + 2 special = 2 tokens <= 10: passthrough, not the skip path.
        results = chunk_long_sections(
            sections, max_tokens=10, overlap_tokens=2,
            count_tokens=whitespace_token_counter(),
        )

        assert len(results) == 1
        chunk, chunk_number = results[0]
        assert chunk_number == 1
        assert chunk["content_text"] == ""
        assert chunk["word_count"] == 0

    def test_long_section_splits(self) -> None:
        """Section exceeding max_tokens is split into multiple chunks."""
        text = " ".join(f"word{i}" for i in range(25))
        sections = [{"content_text": text}]
        results = chunk_long_sections(
            sections, max_tokens=10, overlap_tokens=2, count_tokens=whitespace_token_counter()
        )

        assert len(results) > 1
        # chunk_number should be sequential starting from 1
        chunk_numbers = [cn for _, cn in results]
        assert chunk_numbers == list(range(1, len(results) + 1))

    def test_every_chunk_within_token_budget(self) -> None:
        """No produced chunk exceeds max_tokens under the injected counter."""
        text = " ".join(f"word{i}" for i in range(200))
        sections = [{"content_text": text}]
        count_tokens = whitespace_token_counter()
        results = chunk_long_sections(
            sections, max_tokens=10, overlap_tokens=3, count_tokens=count_tokens
        )

        for chunk, _ in results:
            assert count_tokens(chunk["content_text"]) <= 10

    def test_overlap_between_chunks(self) -> None:
        """Adjacent chunks share overlapping trailing/leading words."""
        words = [f"w{i}" for i in range(40)]
        text = " ".join(words)
        sections = [{"content_text": text}]
        results = chunk_long_sections(
            sections, max_tokens=10, overlap_tokens=3, count_tokens=whitespace_token_counter()
        )

        assert len(results) >= 2
        chunk0_words = results[0][0]["content_text"].split()
        chunk1_words = results[1][0]["content_text"].split()
        # The tail of chunk 0 reappears at the head of chunk 1 (non-empty overlap).
        overlap = [w for w in chunk1_words if w in set(chunk0_words)]
        assert len(overlap) >= 1
        # Overlap is a contiguous suffix of chunk 0 == prefix of chunk 1.
        k = len(overlap)
        assert chunk0_words[-k:] == chunk1_words[:k]

    def test_word_boundaries_preserved(self) -> None:
        """Chunks are composed of whole, unsplit words from the source."""
        words = [f"token{i}" for i in range(50)]
        text = " ".join(words)
        sections = [{"content_text": text}]
        results = chunk_long_sections(
            sections, max_tokens=12, overlap_tokens=4, count_tokens=whitespace_token_counter()
        )

        source_words = set(words)
        for chunk, _ in results:
            for w in chunk["content_text"].split():
                assert w in source_words

    def test_word_count_computed_from_text(self) -> None:
        """word_count reflects whitespace-word count of the chunk, not tokens."""
        sections = [{"content_text": "one two three four five"}]
        results = chunk_long_sections(
            sections, max_tokens=10, overlap_tokens=0, count_tokens=whitespace_token_counter()
        )

        assert results[0][0]["word_count"] == 5

    def test_multiple_sections_mixed(self) -> None:
        """Mix of short and long sections handled correctly."""
        sections = [
            {"content_text": "short text"},
            {"content_text": " ".join(f"w{i}" for i in range(30))},
            {"content_text": "also short"},
        ]
        results = chunk_long_sections(
            sections, max_tokens=10, overlap_tokens=2, count_tokens=whitespace_token_counter()
        )

        # First and last sections pass through; middle one splits into >1 chunk.
        assert results[0][1] == 1  # short passthrough
        assert results[-1][1] == 1  # short passthrough
        assert results[0][0]["content_text"] == "short text"
        assert results[-1][0]["content_text"] == "also short"
        # Middle produced more than one chunk, so total exceeds the 2 short ones.
        assert len(results) > 3

    def test_zero_overlap(self) -> None:
        """Chunks with zero overlap have no shared words."""
        words = [f"w{i}" for i in range(30)]
        text = " ".join(words)
        sections = [{"content_text": text}]
        results = chunk_long_sections(
            sections, max_tokens=10, overlap_tokens=0, count_tokens=whitespace_token_counter()
        )

        assert len(results) >= 2
        seen: set[str] = set()
        for chunk, _ in results:
            chunk_words = chunk["content_text"].split()
            # No word appears in more than one chunk.
            assert seen.isdisjoint(chunk_words)
            seen.update(chunk_words)

    def test_all_words_covered(self) -> None:
        """Every source word appears in at least one chunk, in order."""
        words = [f"w{i}" for i in range(60)]
        text = " ".join(words)
        sections = [{"content_text": text}]
        results = chunk_long_sections(
            sections, max_tokens=10, overlap_tokens=3, count_tokens=whitespace_token_counter()
        )

        covered: set[str] = set()
        for chunk, _ in results:
            covered.update(chunk["content_text"].split())
        assert covered == set(words)

    def test_oversize_single_word_emitted(self) -> None:
        """A single word over the token budget is emitted, not looped/crashed."""
        # Each "word" counts as many tokens via a char-based counter.
        def char_counter(text: str) -> int:
            return len(text.replace(" ", "")) + 1

        sections = [{"content_text": "tiny enormousword tiny"}]
        # max_tokens small enough that 'enormousword' alone exceeds it.
        results = chunk_long_sections(
            sections, max_tokens=6, overlap_tokens=1, count_tokens=char_counter
        )

        all_words: set[str] = set()
        for chunk, _ in results:
            all_words.update(chunk["content_text"].split())
        assert "enormousword" in all_words

    def test_large_overlap_forces_forward_progress(self) -> None:
        """A large overlap relative to max_tokens still terminates with full coverage.

        Exercises the chunker's forward-progress safeguard: with overlap_tokens
        only one below max_tokens the walk-back consumes nearly the whole chunk,
        so the loop must still advance. Asserts the call terminates (no infinite
        loop), produces sequential chunk_numbers, and covers every word in order.
        """
        words = [f"w{i}" for i in range(60)]
        text = " ".join(words)
        sections = [{"content_text": text}]
        # overlap_tokens=9 is just below max_tokens=10: the overlap region nearly
        # swallows the whole chunk, stressing the forward-progress guarantee.
        results = chunk_long_sections(
            sections, max_tokens=10, overlap_tokens=9, count_tokens=whitespace_token_counter()
        )

        # Terminated (we reached this assertion) and split into multiple chunks.
        assert len(results) > 1
        # chunk_numbers are sequential starting from 1.
        chunk_numbers = [cn for _, cn in results]
        assert chunk_numbers == list(range(1, len(results) + 1))
        # Every source word is covered, in order: the concatenation of chunks,
        # deduped to first appearance, equals the original word sequence.
        seen: list[str] = []
        seen_set: set[str] = set()
        for chunk, _ in results:
            for w in chunk["content_text"].split():
                if w not in seen_set:
                    seen.append(w)
                    seen_set.add(w)
        assert seen == words

    @pytest.mark.parametrize(
        "max_tokens, overlap_tokens, match",
        [
            (0, 0, "max_tokens must be positive"),
            (-5, 0, "max_tokens must be positive"),
            (10, -1, "overlap_tokens must be non-negative"),
            (10, 10, "overlap_tokens.*must be less than max_tokens"),
            (10, 15, "overlap_tokens.*must be less than max_tokens"),
        ],
    )
    def test_invalid_token_args_raise(
        self, max_tokens: int, overlap_tokens: int, match: str
    ) -> None:
        """Invalid max_tokens/overlap_tokens combinations raise ValueError."""
        with pytest.raises(ValueError, match=match):
            chunk_long_sections(
                [{"content_text": "test"}],
                max_tokens=max_tokens,
                overlap_tokens=overlap_tokens,
                count_tokens=whitespace_token_counter(),
            )

    def test_missing_content_text_key_raises(self) -> None:
        """Section dict missing 'content_text' raises ValueError."""
        with pytest.raises(ValueError, match="missing required 'content_text' key"):
            chunk_long_sections(
                [{"wrong_key": "test"}],
                max_tokens=10,
                overlap_tokens=2,
                count_tokens=whitespace_token_counter(),
            )

    def test_empty_input_list(self) -> None:
        """Empty input list returns empty results."""
        results = chunk_long_sections(
            [], max_tokens=10, overlap_tokens=2, count_tokens=whitespace_token_counter()
        )
        assert results == []

    def test_single_word_section(self) -> None:
        """Single-word section under budget passes through correctly."""
        sections = [{"content_text": "hello"}]
        results = chunk_long_sections(
            sections, max_tokens=10, overlap_tokens=2, count_tokens=whitespace_token_counter()
        )

        assert len(results) == 1
        assert results[0][0]["content_text"] == "hello"
        assert results[0][0]["word_count"] == 1
        assert results[0][1] == 1
