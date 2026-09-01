"""Shared library for the ingestion workspace.

Subpackages and modules:
  - ``ingpipe_lib.logconfig``: ``setup_logging`` / ``get_logger``
  - ``ingpipe_lib.env``: explicit dotenv loading (``load_env``)
  - ``ingpipe_lib.validators``: SQL identifier and collection-path validation
  - ``ingpipe_lib.sql_comments``: COMMENT ON statement builders
  - ``ingpipe_lib.paths``: instance-root discovery and path anchoring
  - ``ingpipe_lib.db``: the shared engine factory (``get_engine`` /
    ``engine_scope``) and the extension preflight (``require_extensions``)
"""
