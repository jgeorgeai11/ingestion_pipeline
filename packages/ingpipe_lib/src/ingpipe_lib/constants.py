"""Cross-package constants for the ingestion workspace.

Holds the values more than one engine package must agree on. Kept deliberately
tiny: per-module defaults (table names, parse settings) live with the module
that owns them; only genuinely cross-cutting values belong here.
"""

__all__ = ["UINT64_CEILING"]

# Exclusive upper bound for an unsigned 64-bit integer (2**64). Both ingestion
# legs store their source provenance hash (``source_binary_hash``) as a uint64
# in numeric(20,0), and both output validators assert the stored value lies in
# [0, UINT64_CEILING). Defined once so the validators and any future producer
# cannot drift on the ceiling.
UINT64_CEILING = 2**64
