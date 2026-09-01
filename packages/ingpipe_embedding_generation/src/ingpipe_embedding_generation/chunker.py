"""Chunker for splitting long text items into overlapping sub-chunks.

Splits items that exceed a token budget into smaller chunks with
configurable token overlap, packing on word boundaries (a word is never
split). The token count is supplied by the caller via a ``count_tokens``
callable (typically the embedding model's tokenizer), so the budget maps
directly onto the model's context limit including any special tokens the
model adds. Operates on plain dicts with 'content_text' keys, making it
database-agnostic.
"""

from collections.abc import Callable
from typing import TypedDict

from ingpipe_lib.logconfig import get_logger

logger = get_logger(__name__)


class SectionInput(TypedDict):
    """An input section: the text to (possibly) chunk."""

    content_text: str


class SectionChunk(TypedDict):
    """An output chunk: the (sub-)text to embed and its word count."""

    content_text: str
    word_count: int


def chunk_long_sections(
    sections: list[SectionInput],
    max_tokens: int,
    overlap_tokens: int,
    count_tokens: Callable[[str], int],
) -> list[tuple[SectionChunk, int]]:
    """Split sections exceeding max_tokens into sub-chunks with token overlap.

    Sections whose token count is at or below max_tokens are returned
    unchanged with chunk_number=1. Sections exceeding max_tokens are split on
    word boundaries (a word is never split) into multiple chunks, each with a
    token count <= max_tokens. Adjacent chunks share roughly overlap_tokens
    tokens of overlapping content for context continuity.

    Token counts are computed via ``count_tokens``, the model's own tokenizer,
    so the budget maps onto the model's context window (including the special
    tokens the model adds). Each input dict must have a 'content_text' key.

    Args:
        sections: List of dicts, each with at least a 'content_text' key.
        max_tokens: Maximum tokens per chunk. Sections exceeding this are split.
        overlap_tokens: Approximate number of tokens shared between adjacent
            chunks (realized on word boundaries).
        count_tokens: Callable returning the model's token count for a string
            (including special tokens). Used both to decide whether a section
            needs splitting and to pack each chunk to the token budget.

    Returns:
        List of (dict, chunk_number) tuples. Each dict has 'content_text' and
        'word_count' keys (word_count is the number of whitespace-delimited
        words in the chunk). chunk_number is 1 for unsplit sections, and 1..N
        for sub-chunks of split sections. A section that exceeds max_tokens but
        contains no whitespace-delimited words (whitespace-only text) yields NO
        chunks: it is logged as a WARNING and skipped, since it cannot be
        packed on word boundaries and would embed as meaningless whitespace.

    Raises:
        ValueError: If max_tokens <= 0, overlap_tokens < 0,
            overlap_tokens >= max_tokens, or a section dict is missing the
            required 'content_text' key.
    """
    if max_tokens <= 0:
        logger.error(f"max_tokens must be positive, got {max_tokens}")
        raise ValueError(f"max_tokens must be positive, got {max_tokens}")
    if overlap_tokens < 0:
        logger.error(f"overlap_tokens must be non-negative, got {overlap_tokens}")
        raise ValueError(f"overlap_tokens must be non-negative, got {overlap_tokens}")
    if overlap_tokens >= max_tokens:
        logger.error(f"overlap_tokens ({overlap_tokens}) must be less than max_tokens ({max_tokens})")
        raise ValueError(
            f"overlap_tokens ({overlap_tokens}) must be less than max_tokens ({max_tokens})"
        )

    results: list[tuple[SectionChunk, int]] = []
    split_count = 0

    for idx, section in enumerate(sections):
        try:
            content_text = section["content_text"]
        except KeyError:
            raise ValueError(f"Section at index {idx} missing required 'content_text' key") from None

        # Whole-section passthrough when it already fits the token budget.
        if count_tokens(content_text) <= max_tokens:
            results.append(
                ({"content_text": content_text, "word_count": len(content_text.split())}, 1)
            )
            continue

        words = content_text.split()
        # A section can exceed max_tokens yet contain no whitespace-delimited
        # words (e.g. thousands of bare newlines: every character tokenizes,
        # nothing splits). It cannot be packed into word-boundary chunks, and
        # embedding pure whitespace would store a meaningless row — so warn and
        # emit no chunks for it rather than dropping it silently. The caller
        # sees the empty result and can count/report the skip.
        if not words:
            logger.warning(
                f"Section at index {idx} exceeds max_tokens "
                f"({count_tokens(content_text)} tokens) but contains no "
                "whitespace-delimited words (whitespace-only text); "
                "emitting no chunks for it"
            )
            continue
        chunks = _split_words_by_tokens(words, max_tokens, overlap_tokens, count_tokens)
        for chunk_number, chunk_words in enumerate(chunks, start=1):
            chunk_text = " ".join(chunk_words)
            results.append(
                ({"content_text": chunk_text, "word_count": len(chunk_words)}, chunk_number)
            )

        split_count += 1
        logger.debug(
            f"Section {idx} split into {len(chunks)} chunks "
            f"({count_tokens(content_text)} tokens)"
        )

    # DEBUG, not INFO: this runs once per call, and the embedder calls it once
    # per source row (hundreds of thousands of times in a full run). The caller
    # logs the aggregate ("Chunked N rows into M chunks") at INFO instead.
    logger.debug(
        f"Chunking complete: {len(sections)} sections -> {len(results)} chunks "
        f"({split_count} sections split)"
    )
    return results


def _split_words_by_tokens(
    words: list[str],
    max_tokens: int,
    overlap_tokens: int,
    count_tokens: Callable[[str], int],
) -> list[list[str]]:
    """Pack a word list into overlapping chunks each within the token budget.

    Greedily packs words into a chunk until adding the next word would exceed
    max_tokens (measured by ``count_tokens`` on the joined text, so the model's
    special tokens count against the budget). The next chunk starts overlap_tokens
    tokens back from the chunk's end, realized on a word boundary.

    A single word whose own token count exceeds max_tokens is emitted as its own
    oversize chunk (it cannot be packed within budget without splitting the word,
    which is disallowed). At max_tokens=500 this does not occur for real text.

    Args:
        words: Whitespace-split words of the section (non-empty for a split section).
        max_tokens: Maximum tokens per chunk.
        overlap_tokens: Approximate token overlap between adjacent chunks.
        count_tokens: Callable returning the model's token count for a string.

    Returns:
        List of chunks, each a list of words. Every chunk is non-empty and the
        union of chunks covers every word in order.
    """
    chunks: list[list[str]] = []
    start = 0
    n = len(words)

    while start < n:
        # Greedily extend the chunk one word at a time while it fits the budget.
        # The joined slice is re-tokenized on every extension (O(words^2) tokenizer
        # calls per chunk) rather than kept as a running total, because the
        # tokenizer is not additive across word boundaries (subword merges +
        # special tokens), so a per-word token sum would not match the real count.
        # Bounded in practice: at max_tokens=500 a chunk holds only a few hundred
        # words.
        end = start
        while end < n and count_tokens(" ".join(words[start : end + 1])) <= max_tokens:
            end += 1

        # Always emit at least one word so an oversize single word can't stall the loop.
        if end == start:
            end = start + 1

        chunk_words = words[start:end]
        chunks.append(chunk_words)

        if end >= n:
            break

        # Determine the next start by walking back from the chunk end until the
        # trailing words reach the overlap token budget; this realizes the
        # token overlap on a word boundary.
        next_start = end
        while (
            next_start > start + 1
            and count_tokens(" ".join(words[next_start - 1 : end])) <= overlap_tokens
        ):
            next_start -= 1

        # Forward progress is guaranteed without an extra guard: the walk-back
        # loop's `next_start > start + 1` condition floors next_start at
        # start + 1, so start strictly increases each iteration and the outer
        # loop always terminates.
        start = next_start

    return chunks
