"""Shared utilities for embedding generation scripts."""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

from ingpipe_lib.logconfig import get_logger

# Re-exported so ingpipe_embedding_generation's call sites and tests keep importing
# this name from _utils; the canonical copy lives in ingpipe_lib.validators
# (the ``fullmatch`` implementation, so it rejects a trailing newline that the
# old local ``.match`` copy accepted).
from ingpipe_lib.validators import validate_sql_identifier

__all__ = ["get_tokenizer", "make_token_counter", "validate_sql_identifier"]

logger = get_logger(__name__)


def get_tokenizer(model: "SentenceTransformer") -> Any:
    """Return the underlying HuggingFace tokenizer of a SentenceTransformer.

    The tokenizer is exposed as ``model.tokenizer`` on a SentenceTransformer;
    when that attribute is absent (older/custom module layouts), it lives on
    the first module (``model._first_module().tokenizer``). Resolving it in one
    place guarantees the chunker and the output validator count tokens
    identically.

    Args:
        model: A loaded SentenceTransformer instance.

    Returns:
        The HuggingFace tokenizer used by the model.

    Raises:
        AttributeError: If no tokenizer can be located on the model.
    """
    tokenizer = getattr(model, "tokenizer", None)
    if tokenizer is None:
        logger.debug(
            "model has no .tokenizer attribute; falling back to "
            "_first_module().tokenizer"
        )
        tokenizer = model._first_module().tokenizer
    return tokenizer


def make_token_counter(model: "SentenceTransformer") -> Callable[[str], int]:
    """Build a ``count_tokens`` callable from a model's tokenizer.

    The returned callable reports the model's token count for a string,
    including the special tokens the model adds, by tokenizing without
    truncation and counting ``input_ids``. This is the single token-counting
    convention shared by the chunker (to budget chunks) and the output
    validator (to prove no stored chunk exceeds the budget).

    Args:
        model: A loaded SentenceTransformer instance.

    Returns:
        A callable mapping a string to its model token count (special tokens
        included, no truncation).
    """
    tokenizer = get_tokenizer(model)

    def count_tokens(text: str) -> int:
        return len(tokenizer(text, truncation=False)["input_ids"])

    return count_tokens
