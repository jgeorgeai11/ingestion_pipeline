"""The download-time manifest: what a run RESOLVED to fetch, and what it produced.

Before this module existed, nothing in the pipeline recorded a run's intent.
Every acquisition validator could therefore only ask "is there a file here?",
which a 1-of-40-chapter download answers just as happily as a complete one.
The manifest closes that gap: the runner writes one entry per **discovered
target** on every run, and validation compares the corpus on disk against that
record instead of against its own guess.

Three properties make the record trustworthy, and each is load-bearing:

  - **Artifacts, not bytes.** An entry records the paths a target *resolved
    to* — for a post-processed target the post-processor's returned files
    (the extracted PDFs), not the archive that no longer exists. Validation
    then checks what the rest of the pipeline actually consumes.
  - **The complete discovered set.** A target skipped because it was already
    present is recorded exactly as a freshly fetched one; a target that
    FAILED is recorded as failed rather than dropped. A resume run that
    fetches 37 of 40 still writes a 40-entry manifest, so validation sees the
    three holes instead of a shrunk-but-self-consistent set.
  - **Relative destinations.** Paths are stored relative to the output root,
    so moving an instance directory does not invalidate its manifests.

A missing or malformed manifest raises :class:`ManifestError` rather than
returning an empty list: "no manifest" must never read as "nothing to check".
"""

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ingpipe_lib.logconfig import get_logger

__all__ = [
    "MANIFEST_FILENAME",
    "STATUS_FAILED",
    "STATUS_FETCHED",
    "STATUS_SKIPPED",
    "Artifact",
    "ManifestEntry",
    "ManifestError",
    "Target",
    "index_by_url",
    "manifest_path",
    "read_manifest",
    "write_manifest",
]

logger = get_logger(__name__)

# One manifest per output root, overwritten by each run. The leading dot keeps
# it out of the way of the corpus files ingestion globs over.
MANIFEST_FILENAME = ".acquisition_manifest.json"

# The three states an entry can be in. "fetched" and "skipped" are both
# successes (the artifacts are present either way); "failed" records a hole.
STATUS_FETCHED = "fetched"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"


class ManifestError(Exception):
    """Raised when a manifest is absent, unreadable, or structurally invalid.

    Typed so the validator can distinguish "this corpus was never acquired by
    a manifest-writing run" from an ordinary I/O error, and so the runner can
    treat absence as "no prior run" deliberately rather than by catching
    whatever happened to come out.
    """


@dataclass(frozen=True)
class Target:
    """One thing to acquire: a URL and where its bytes belong.

    Frozen because a discoverer yields targets into a run loop that must not
    be able to rewrite them — the destination is security-relevant (a scraped
    page influences it), so it is validated once and then immutable.

    Attributes:
        url: The absolute URL to fetch.
        destination: Where the fetched bytes go, RELATIVE to the run's output
            root. A relative path is required so the escape check has
            something to check and so manifests survive a directory move.
        group: An optional logical grouping (e.g. the manual folder a chapter
            belongs to), carried into the manifest for reporting only.
        expected_size: An optional byte size the caller already knows, used as
            an extra post-fetch assertion when the server sends no
            ``Content-Length``.
    """

    url: str
    destination: Path
    group: str | None = None
    expected_size: int | None = None


@dataclass(frozen=True)
class Artifact:
    """One file a target produced, with the size recorded at production time.

    Attributes:
        path: The artifact's path relative to the output root.
        size: The artifact's size in bytes as observed when it was written.
    """

    path: Path
    size: int


@dataclass
class ManifestEntry:
    """The record of one discovered target's outcome in one run.

    Attributes:
        url: The target's source URL, which is also the key the next run's
            skip decision looks up.
        destination: The target's destination relative to the output root.
        group: The target's optional grouping label.
        status: One of ``"fetched"``, ``"skipped"``, or ``"failed"``.
        artifacts: The files this target resolved to. Empty for a failed
            entry; for a post-processed target these are the post-processor's
            outputs rather than the downloaded archive.
        error: The failure message when ``status`` is ``"failed"``.
    """

    url: str
    destination: Path
    group: str | None = None
    status: str = STATUS_FETCHED
    artifacts: list[Artifact] = field(default_factory=list)
    error: str | None = None


def _entry_to_json(entry: ManifestEntry) -> dict:
    """Render one entry as the JSON object shape stored on disk.

    Args:
        entry: The entry to serialize.

    Returns:
        A JSON-serializable dict with POSIX-style relative path strings, so a
        manifest written on one platform reads identically on another.
    """
    return {
        "url": entry.url,
        "destination": entry.destination.as_posix(),
        "group": entry.group,
        "status": entry.status,
        "error": entry.error,
        "artifacts": [
            {"path": artifact.path.as_posix(), "size": artifact.size}
            for artifact in entry.artifacts
        ],
    }


def _entry_from_json(raw: object, index: int) -> ManifestEntry:
    """Parse one stored entry, raising :class:`ManifestError` on any deviation.

    Args:
        raw: The decoded JSON value at position `index` of the entries array.
        index: The entry's position, named in error messages so a malformed
            manifest points at the offending record.

    Returns:
        The parsed entry.

    Raises:
        ManifestError: If the entry is not an object, is missing ``url`` or
            ``destination``, carries an unknown status, or has a malformed
            artifact list. Every deviation is fatal: a manifest that cannot be
            read in full cannot support the completeness claim it exists for.
    """
    if not isinstance(raw, dict):
        raise ManifestError(f"Manifest entry {index} is {type(raw).__name__}, expected an object")

    url = raw.get("url")
    destination = raw.get("destination")
    if not isinstance(url, str) or not url:
        raise ManifestError(f"Manifest entry {index} has a missing or non-string 'url'")
    if not isinstance(destination, str) or not destination:
        raise ManifestError(f"Manifest entry {index} ({url}) has a missing 'destination'")

    status = raw.get("status", STATUS_FETCHED)
    if status not in (STATUS_FETCHED, STATUS_SKIPPED, STATUS_FAILED):
        raise ManifestError(f"Manifest entry {index} ({url}) has unknown status {status!r}")

    group = raw.get("group")
    if group is not None and not isinstance(group, str):
        raise ManifestError(f"Manifest entry {index} ({url}) has a non-string 'group'")

    error = raw.get("error")
    if error is not None and not isinstance(error, str):
        raise ManifestError(f"Manifest entry {index} ({url}) has a non-string 'error'")

    raw_artifacts = raw.get("artifacts", [])
    if not isinstance(raw_artifacts, list):
        raise ManifestError(f"Manifest entry {index} ({url}) has a non-list 'artifacts'")

    artifacts: list[Artifact] = []
    for position, raw_artifact in enumerate(raw_artifacts):
        if not isinstance(raw_artifact, dict):
            raise ManifestError(
                f"Manifest entry {index} ({url}) artifact {position} is not an object"
            )
        path = raw_artifact.get("path")
        size = raw_artifact.get("size")
        if not isinstance(path, str) or not path:
            raise ManifestError(
                f"Manifest entry {index} ({url}) artifact {position} has a missing 'path'"
            )
        # bool is an int subclass, so it is rejected explicitly rather than
        # silently accepted as a size of 0 or 1.
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ManifestError(
                f"Manifest entry {index} ({url}) artifact {path} has a non-integer 'size'"
            )
        artifacts.append(Artifact(path=Path(path), size=size))

    return ManifestEntry(
        url=url,
        destination=Path(destination),
        group=group,
        status=status,
        artifacts=artifacts,
        error=error,
    )


def manifest_path(output_root: Path) -> Path:
    """Return the manifest location for an output root.

    Args:
        output_root: The run's resolved output root directory.

    Returns:
        The path of the manifest file inside that root.
    """
    return output_root / MANIFEST_FILENAME


def write_manifest(output_root: Path, entries: Iterable[ManifestEntry]) -> Path:
    """Write the run's manifest, replacing any prior one.

    The write is atomic (a temporary file replaced onto the final name), so an
    interrupted write cannot leave a half-written manifest that the next run's
    skip logic or the validator would then reject or, worse, misread.

    Args:
        output_root: The run's output root; created if absent.
        entries: One entry per DISCOVERED target — fetched, skipped, and
            failed alike. Passing only the targets fetched this run would
            reintroduce the resume hole this file exists to close.

    Returns:
        The path written.

    Raises:
        ManifestError: If the manifest cannot be written.
    """
    entry_list = list(entries)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "entries": [_entry_to_json(entry) for entry in entry_list],
    }

    destination = manifest_path(output_root)
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        output_root.mkdir(parents=True, exist_ok=True)
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=False)
            handle.write("\n")
        temporary.replace(destination)
    except OSError as e:
        temporary.unlink(missing_ok=True)
        logger.error(f"Failed to write manifest {destination}: {e}")
        raise ManifestError(f"Failed to write manifest {destination}: {e}") from e

    logger.info(f"Wrote manifest with {len(entry_list)} entries: {destination}")
    return destination


def read_manifest(output_root: Path) -> list[ManifestEntry]:
    """Read the manifest at an output root.

    Args:
        output_root: The output root whose manifest to read.

    Returns:
        The recorded entries, in the order the writing run discovered them.

    Raises:
        ManifestError: If the manifest is absent, unreadable, not valid JSON,
            or structurally invalid. Absence is an error rather than an empty
            list because the validator's whole purpose is to detect
            incompleteness — a vacuous pass would be the loudest possible
            version of the bug it replaces. Callers that legitimately tolerate
            absence (the runner deciding whether a PRIOR run exists) check for
            the file first via :func:`manifest_path`.
    """
    source = manifest_path(output_root)
    if not source.is_file():
        raise ManifestError(
            f"No acquisition manifest at {source}: nothing recorded what this "
            "corpus was supposed to contain, so it cannot be validated. Run the "
            "acquisition entry point for this source first."
        )

    try:
        with open(source, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f"Failed to read manifest {source}: {e}")
        raise ManifestError(f"Malformed acquisition manifest {source}: {e}") from e

    if not isinstance(payload, dict):
        raise ManifestError(f"Malformed acquisition manifest {source}: top level is not an object")

    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise ManifestError(f"Malformed acquisition manifest {source}: 'entries' is not a list")

    entries = [_entry_from_json(raw, index) for index, raw in enumerate(raw_entries)]
    logger.debug(f"Read manifest with {len(entries)} entries: {source}")
    return entries


def index_by_url(entries: Sequence[ManifestEntry]) -> dict[str, ManifestEntry]:
    """Index entries by source URL for the runner's skip decision.

    The URL is the stable key across runs: a target's destination can change
    when a discoverer's naming rule is corrected, but the URL it came from
    does not.

    Args:
        entries: The entries to index.

    Returns:
        A mapping of URL to entry; on a duplicate URL the last entry wins,
        which matches the runner's own last-write-wins ordering.
    """
    return {entry.url: entry for entry in entries}
