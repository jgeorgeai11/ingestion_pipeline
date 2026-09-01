"""Shared DDL template rendering for the ingestion legs.

Both ingestion legs ship a ``.sql`` template with ``{placeholder}`` slots for
the validated schema/table identifiers. The read-substitute-verify sequence
was duplicated (and had drifted in whether it logged on failure); this is the
one implementation: read the template (UTF-8 — the comment texts carry
non-ASCII em dashes that the platform default would mojibake on Windows),
substitute every provided placeholder, and fail loudly if any ``{...}``
survives — template/code drift would otherwise surface later as an obscure
DDL execution error.

Substitution only: the caller validates the identifiers (they are
interpolated into DDL) and owns execution, transactions, and COMMENT ON
overrides.
"""

import re
from pathlib import Path

from ingpipe_lib.logconfig import get_logger

__all__ = ["render_ddl_template"]

logger = get_logger(__name__)


def render_ddl_template(ddl_path: Path, substitutions: dict[str, str]) -> str:
    """Render a DDL template by substituting its ``{placeholder}`` slots.

    Args:
        ddl_path: Path to the ``.sql`` template file.
        substitutions: Mapping of placeholder name (without braces) to the
            validated identifier to substitute (e.g.
            ``{"schema_name": "cms_iom"}``).

    Returns:
        The rendered DDL string with every placeholder substituted.

    Raises:
        FileNotFoundError: If ``ddl_path`` does not exist.
        OSError: If the template cannot be read.
        ValueError: If the rendered DDL still contains a ``{...}`` placeholder
            (template/code drift: the template introduced a placeholder the
            caller does not substitute).
    """
    if not ddl_path.exists():
        logger.error(f"DDL template not found: {ddl_path}")
        raise FileNotFoundError(f"DDL template not found: {ddl_path}")

    try:
        rendered = ddl_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.error(f"Failed to read DDL template: {ddl_path} - {e}")
        raise

    for placeholder, value in substitutions.items():
        rendered = rendered.replace("{" + placeholder + "}", value)

    # Fail loudly on template/code drift: a surviving {...} means the template
    # introduced a placeholder this call does not substitute, which would
    # otherwise fail later with an obscure DDL execution error.
    leftover = re.findall(r"\{[^}]*\}", rendered)
    if leftover:
        logger.error(
            f"Unsubstituted placeholder(s) in rendered DDL from {ddl_path}: "
            f"{leftover}"
        )
        raise ValueError(
            f"Unsubstituted placeholder(s) in rendered DDL from {ddl_path}: "
            f"{leftover}"
        )

    logger.debug(f"Rendered DDL template {ddl_path} ({len(rendered)} chars)")
    return rendered
