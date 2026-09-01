"""Shared identifier and path validators.

The canonical copies of ``validate_sql_identifier`` and
``validate_collection_path``: pure, dependency-free regex validators shared
by the ingestion packages and the MCP server. Kept in ``ingpipe-lib`` so
every package imports one implementation instead of carrying its own copy.
"""

import re

__all__ = ["validate_collection_path", "validate_sql_identifier"]

# Regex for safe SQL identifiers (lowercase letters, digits, underscores; must start with letter or underscore)
_SAFE_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

# Regex for a valid lowercase ``ltree`` value: dot-separated labels, each one or
# more of [a-z0-9_], with no empty labels (no leading/trailing/double dots), no
# uppercase, and no other characters.
_LTREE_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")


def validate_sql_identifier(name: str, label: str) -> str:
    """Validate that a string is a safe SQL identifier to prevent injection.

    Args:
        name: The identifier string to validate.
        label: Descriptive label for error messages (e.g., "db_schema").

    Returns:
        The validated identifier string.

    Raises:
        ValueError: If the identifier contains unsafe characters.
    """
    # fullmatch (not match): with re.match, the anchored ``$`` would also match
    # just before a trailing newline, so "public\n" would pass. fullmatch requires
    # the entire string to match.
    if not _SAFE_IDENTIFIER_RE.fullmatch(name):
        raise ValueError(
            f"Unsafe SQL identifier for {label}: {name!r}. "
            "Must match pattern [a-z_][a-z0-9_]*"
        )
    return name


def validate_collection_path(path: str) -> str:
    """Validate that a config-authored collection path is a valid ``ltree`` value.

    A valid value is a dot-delimited ``ltree`` whose every label contains only
    ``[a-z0-9_]``. Concretely it must match
    ``^[a-z0-9_]+(\\.[a-z0-9_]+)*$`` — one or more dot-separated labels, each a
    non-empty run of lowercase letters, digits, and underscores, with no empty
    labels (no leading, trailing, or doubled dots), no uppercase, and no other
    characters.

    This is a pure validator: it transforms nothing. A valid path is returned
    unchanged; anything else (uppercase, dashes, spaces, a ``.ext`` leaf, an
    empty label, or an empty/blank string) is rejected so the caller can skip
    the offending document rather than silently rewriting its identity.

    Args:
        path: The config-authored collection path (dot-delimited).

    Returns:
        ``path`` unchanged, when it is a valid lowercase ``ltree`` value.

    Raises:
        ValueError: If ``path`` is not a valid lowercase ``ltree`` value (the
            message names the offending path).
    """
    # fullmatch (not match): with re.match, the anchored ``$`` would also match
    # just before a trailing newline, so "a.b\n" would pass. fullmatch requires
    # the entire string to match.
    if not _LTREE_RE.fullmatch(path):
        raise ValueError(
            f"Invalid collection_path {path!r}: must be a lowercase ltree value "
            "matching ^[a-z0-9_]+(\\.[a-z0-9_]+)*$ "
            "(dot-separated labels of [a-z0-9_], no empty labels, no uppercase)"
        )
    return path
