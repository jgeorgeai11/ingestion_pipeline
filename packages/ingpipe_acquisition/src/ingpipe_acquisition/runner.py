"""The acquisition run loop and the generic, config-only entry point.

What the runner owns that neither former downloader did:

  - **A non-zero exit on failure.** Both old scripts logged
    ``"... N errors"`` and exited 0, so a scheduled run that fetched nothing
    looked exactly like one that fetched everything. Failures accumulate here
    and are handed to ``ingpipe_lib.cli.finish_run``.
  - **A manifest-driven skip.** The USC script decided "already downloaded" by
    testing the extract directory -- which it created *before* extracting, so
    an interrupted extraction wedged that title permanently. The CMS script
    skipped a whole manual whose folder was non-empty, so an interruption at
    chapter 3 of 40 left the other 37 unreachable. Here the question is asked
    per target against the PRIOR run's manifest: "are this target's recorded
    artifacts all present at their recorded sizes?"
  - **A discovery floor.** ``min_targets`` turns a markup change that makes
    the link pattern match nothing into a loud failure instead of
    ``Found 0 manual page links`` at INFO followed by exit 0.
  - **A destination-escape check.** A scraped page influences the destination,
    so every resolved path is required to stay under the output root.

The entry point is generic: a source whose targets can be COMPUTED (an
explicit list, or a URL template over a substitution list) needs no code at
all -- its instance declares a console script pointing straight at
:func:`main`. A source whose targets must be FOUND supplies a ``discover``
callable and calls :func:`main` from a three-line wrapper.
"""

import sys
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

import requests
from ingpipe_lib.cli import build_parser, finish_run, load_config, run_scope, setup_entry_logging
from ingpipe_lib.logconfig import get_logger
from ingpipe_lib.paths import InstanceRootNotFoundError, is_rooted_path, resolve_config_path

from ingpipe_acquisition.discover import build_discoverer
from ingpipe_acquisition.extract import build_post_processor
from ingpipe_acquisition.fetch import FetchError, build_session, fetch
from ingpipe_acquisition.manifest import (
    STATUS_FAILED,
    STATUS_FETCHED,
    STATUS_SKIPPED,
    Artifact,
    ManifestEntry,
    ManifestError,
    Target,
    index_by_url,
    manifest_path,
    read_manifest,
    write_manifest,
)

__all__ = [
    "Discoverer",
    "PostProcessor",
    "main",
    "run_acquisition",
    "validate_config",
]

logger = get_logger(__name__)

# The log subdirectory every acquisition run writes under, mirroring the
# module path the way the rest of the workspace does.
LOG_SUBDIR = "ingpipe_acquisition"

#: A discoverer takes the run's config and yields the targets to acquire.
#: Config-driven implementations live in :mod:`ingpipe_acquisition.discover`; a source
#: whose targets can only be scraped supplies its own callable with this exact
#: signature, which is the whole instance-facing contract.
Discoverer = Callable[[dict], Iterable[Target]]

#: A post-processor turns one arrival into the artifacts it resolves to,
#: returning their paths. It is called once per fetched target with the target
#: and the path the bytes landed at, and it must raise (not return an empty
#: list) when the arrival is unusable. Cross-target work -- "combine these
#: forty PDFs" -- is a pipeline stage, not post-processing.
PostProcessor = Callable[[Target, Path], Sequence[Path]]


def validate_config(config: dict) -> None:
    """Validate the acquisition config's shared keys, failing at load time.

    Checks only the keys the runner itself reads; the discoverer and the
    post-processor validate their own sections. Every problem is a
    ``ValueError`` so the entry point has one abort path.

    Args:
        config: The parsed TOML config.

    Raises:
        ValueError: If ``output_dir`` is missing or not a string, or any
            optional key is present with the wrong type or an impossible
            value (a negative delay, a ``min_targets`` below 1).
    """
    output_dir = config.get("output_dir")
    if not isinstance(output_dir, str) or not output_dir:
        raise ValueError("Config key 'output_dir' is required and must be a non-empty string")

    min_targets = config.get("min_targets", 1)
    if not isinstance(min_targets, int) or isinstance(min_targets, bool) or min_targets < 1:
        raise ValueError(
            f"Config key 'min_targets' must be an integer >= 1, got {min_targets!r}. "
            "A floor of at least 1 is what turns a discovery that finds nothing "
            "into a failure instead of a silent success."
        )

    delay = config.get("request_delay_seconds", 1.0)
    if not isinstance(delay, int | float) or isinstance(delay, bool) or delay < 0:
        raise ValueError(f"Config key 'request_delay_seconds' must be a number >= 0, got {delay!r}")

    for key in ("dry_run", "overwrite"):
        value = config.get(key, False)
        if not isinstance(value, bool):
            raise ValueError(f"Config key '{key}' must be a boolean, got {value!r}")

    max_bytes = config.get("max_bytes")
    if max_bytes is not None and (
        not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0
    ):
        raise ValueError(f"Config key 'max_bytes' must be a positive integer, got {max_bytes!r}")

    http = config.get("http", {})
    if not isinstance(http, dict):
        raise ValueError(f"Config table '[http]' must be a table, got {http!r}")
    retries = http.get("retries", 3)
    if not isinstance(retries, int) or isinstance(retries, bool) or retries < 0:
        raise ValueError(f"Config key 'http.retries' must be an integer >= 0, got {retries!r}")

    # backoff_factor may legitimately be 0 (retry immediately); timeout may not.
    backoff = http.get("backoff_factor", 0.5)
    if not isinstance(backoff, int | float) or isinstance(backoff, bool) or backoff < 0:
        raise ValueError(f"Config key 'http.backoff_factor' must be a number >= 0, got {backoff!r}")
    timeout = http.get("timeout", 60.0)
    if not isinstance(timeout, int | float) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError(f"Config key 'http.timeout' must be a positive number, got {timeout!r}")


def resolve_destination(output_root: Path, destination: Path) -> Path:
    """Resolve a target's destination under the output root, rejecting escapes.

    The destination for a scraped source is derived from remote content, so it
    is untrusted input: a ``..`` component or an absolute path would let a
    compromised or merely malformed page write anywhere the process can reach.

    Args:
        output_root: The run's resolved output root.
        destination: The target's destination, expected to be relative.

    Returns:
        The absolute path under `output_root`.

    Raises:
        ValueError: If `destination` is rooted or resolves outside
            `output_root`.
    """
    # This site holds a Path, so the value is rendered with as_posix() rather
    # than str() -- str() on a Windows Path renders "/etc/passwd" back as
    # "\etc\passwd" -- and backslashes are normalized so an authored "\x" is
    # judged the same on both hosts. The escape check below already blocked
    # the write; what was host-dependent here was which error the operator got.
    if is_rooted_path(destination.as_posix().replace("\\", "/")):
        raise ValueError(f"Target destination must be relative to the output root: {destination}")

    # Resolve both sides so that symlinked roots and ".." components compare
    # on the same footing.
    root = output_root.resolve()
    candidate = (root / destination).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(
            f"Target destination {destination} escapes the output root {output_root}"
        )
    return candidate


def _relative_to_root(path: Path, output_root: Path) -> Path:
    """Express an absolute artifact path relative to the output root.

    Args:
        path: The absolute artifact path.
        output_root: The run's output root.

    Returns:
        The path relative to `output_root`.

    Raises:
        ValueError: If `path` is not under `output_root`, which would mean a
            post-processor wrote outside the corpus.
    """
    try:
        return path.resolve().relative_to(output_root.resolve())
    except ValueError as e:
        raise ValueError(
            f"Produced artifact {path} lies outside the output root {output_root}"
        ) from e


def _artifacts_intact(entry: ManifestEntry, output_root: Path) -> bool:
    """Check whether a prior entry's recorded artifacts are all still correct.

    This is the whole skip decision. It asks about the artifacts a target
    RESOLVED to -- the extracted PDFs, not the archive that was deleted --
    which is exactly the question the old USC code got wrong by testing an
    extract directory it had created before extracting.

    Args:
        entry: The prior run's entry for this target.
        output_root: The run's output root.

    Returns:
        True when the entry succeeded, recorded at least one artifact, and
        every artifact still exists at its recorded byte size.
    """
    if entry.status not in (STATUS_FETCHED, STATUS_SKIPPED) or not entry.artifacts:
        return False
    for artifact in entry.artifacts:
        path = output_root / artifact.path
        if not path.is_file() or path.stat().st_size != artifact.size:
            logger.debug(f"Prior artifact missing or resized, will re-fetch: {path}")
            return False
    return True


def _load_prior_manifest(output_root: Path) -> dict[str, ManifestEntry]:
    """Load the previous run's manifest, tolerating its absence.

    The validator treats a missing manifest as a hard failure; the runner must
    not, because the first run of a source legitimately has none. Absence is
    therefore checked explicitly rather than by swallowing
    :class:`~ingpipe_acquisition.manifest.ManifestError`, so a *malformed* manifest is
    still reported.

    Args:
        output_root: The run's output root.

    Returns:
        A URL-keyed index of the prior entries, empty when no prior run exists.
    """
    if not manifest_path(output_root).is_file():
        logger.info(f"No prior manifest at {output_root}: treating every target as new")
        return {}
    try:
        return index_by_url(read_manifest(output_root))
    except ManifestError as e:
        # A corrupt manifest must not silently become a full re-download that
        # then overwrites it; the operator gets told, and every target is
        # re-fetched deliberately.
        logger.warning(f"Ignoring unreadable prior manifest: {e}")
        return {}


def _delete_artifacts(paths: Iterable[Path]) -> None:
    """Remove the files a failed target produced, so it leaves no residue.

    Args:
        paths: Absolute paths to remove. Missing paths are ignored; a
            directory is left alone (post-processors return files).
    """
    for path in paths:
        try:
            if path.is_file():
                path.unlink()
                logger.debug(f"Removed artifact of failed target: {path}")
        except OSError as e:
            logger.warning(f"Could not remove {path} after a failed target: {e}")


def _collect_targets(config: dict, discover: Discoverer, output_root: Path) -> list[Target]:
    """Enumerate and validate every target before anything is fetched.

    The discoverers are generators, but the run consumes them eagerly: both
    ``min_targets`` and the escape check are whole-set questions that must be
    answered BEFORE the first byte is written, or a broken discovery would be
    caught only after it had already half-populated the corpus.

    Args:
        config: The run's config, passed through to the discoverer.
        discover: The discoverer callable.
        output_root: The run's output root, for the escape check.

    Returns:
        The discovered targets, in discovery order.

    Raises:
        ValueError: If the discoverer yields fewer than ``min_targets``
            targets, if two targets resolve to the same destination, or if any
            destination escapes the output root.
    """
    targets = list(discover(config))
    logger.info(f"Discovered {len(targets)} target(s)")

    seen: dict[Path, str] = {}
    for target in targets:
        resolved = resolve_destination(output_root, target.destination)
        if resolved in seen and seen[resolved] != target.url:
            raise ValueError(
                f"Two targets resolve to the same destination {target.destination}: "
                f"{seen[resolved]} and {target.url}. A collision would silently drop "
                "one of them."
            )
        seen[resolved] = target.url

    min_targets = int(config.get("min_targets", 1))
    if len(targets) < min_targets:
        raise ValueError(
            f"Discovery yielded {len(targets)} target(s), below the configured "
            f"min_targets={min_targets}. A source that suddenly resolves to nothing "
            "is a broken discovery, not an empty corpus."
        )
    return targets


def _log_dry_run(targets: Sequence[Target], output_root: Path) -> None:
    """Log every resolved target and destination without fetching anything.

    Args:
        targets: The discovered targets.
        output_root: The run's output root.
    """
    for target in targets:
        logger.info(f"[DRY RUN] {target.url} -> {output_root / target.destination}")
    logger.info(f"[DRY RUN] {len(targets)} target(s) resolved; nothing fetched, no manifest written")


def run_acquisition(
    config: dict,
    config_path: str | Path,
    *,
    discover: Discoverer,
    post_process: PostProcessor | None = None,
) -> list[str]:
    """Acquire every discovered target, returning the run's failure messages.

    A failure in one target fails only that target: its artifacts are deleted,
    it is recorded in the manifest as ``failed`` (never dropped, so validation
    sees the hole), and the run continues. The caller turns a non-empty return
    value into a non-zero exit.

    Args:
        config: The parsed TOML config, already validated by
            :func:`validate_config`.
        config_path: The ``--config`` path, used to anchor ``output_dir`` to
            the instance root.
        discover: The callable yielding this source's targets.
        post_process: An optional per-arrival hook returning the artifact paths
            the target resolved to. When None a target resolves to its own
            destination.

    Returns:
        One message per failed target; empty on a clean run.

    Raises:
        ValueError: If discovery fails the ``min_targets`` floor or a
            destination escapes the output root. These are whole-run failures,
            not per-target ones, and they are raised before anything is
            fetched.
        InstanceRootNotFoundError: If a relative ``output_dir`` has no instance
            root to anchor to.
    """
    output_root = resolve_config_path(config["output_dir"], config_path)
    dry_run = bool(config.get("dry_run", False))
    overwrite = bool(config.get("overwrite", False))
    delay = float(config.get("request_delay_seconds", 1.0))
    max_bytes = config.get("max_bytes")
    http = config.get("http", {})

    logger.info(
        f"Acquisition run: output_root={output_root}, dry_run={dry_run}, "
        f"overwrite={overwrite}, min_targets={config.get('min_targets', 1)}"
    )

    # min_targets and the escape check run even for a dry run: a dry run whose
    # only output is "0 targets, exit 0" would hide the exact breakage those
    # checks exist to surface.
    targets = _collect_targets(config, discover, output_root)

    if dry_run:
        _log_dry_run(targets, output_root)
        return []

    prior = {} if overwrite else _load_prior_manifest(output_root)
    entries: list[ManifestEntry] = []
    failures: list[str] = []
    fetched = skipped = 0

    with build_session(
        retries=int(http.get("retries", 3)),
        backoff_factor=float(http.get("backoff_factor", 0.5)),
        timeout=float(http.get("timeout", 60.0)),
    ) as session:
        for target in targets:
            previous = prior.get(target.url)
            if previous is not None and _artifacts_intact(previous, output_root):
                logger.info(f"Skipping (already acquired): {target.url}")
                entries.append(
                    ManifestEntry(
                        url=target.url,
                        destination=target.destination,
                        group=target.group,
                        status=STATUS_SKIPPED,
                        artifacts=list(previous.artifacts),
                    )
                )
                skipped += 1
                continue

            # The polite delay belongs before a request, not before a skip: a
            # resume run that skips 37 of 40 targets should not sleep 37 times.
            # It is also not paid before the first fetch of the run.
            if fetched or failures:
                time.sleep(delay)

            entry, failure = _acquire_one(
                target,
                output_root=output_root,
                session=session,
                max_bytes=max_bytes,
                post_process=post_process,
            )
            entries.append(entry)
            if failure is None:
                fetched += 1
            else:
                failures.append(failure)

    write_manifest(output_root, entries)
    logger.info(
        f"Acquisition complete: {fetched} fetched, {skipped} skipped, "
        f"{len(failures)} failed, {len(entries)} recorded in the manifest"
    )
    return failures


def _acquire_one(
    target: Target,
    *,
    output_root: Path,
    session: requests.Session,
    max_bytes: int | None,
    post_process: PostProcessor | None,
) -> tuple[ManifestEntry, str | None]:
    """Fetch and post-process one target, converting any failure into an entry.

    Args:
        target: The target to acquire.
        output_root: The run's output root.
        session: The shared ``requests.Session``.
        max_bytes: The optional per-response ceiling.
        post_process: The optional per-arrival hook.

    Returns:
        A ``(entry, failure)`` pair. On success `failure` is None and the entry
        carries the produced artifacts; on failure the entry is recorded with
        status ``failed`` so validation sees the hole, and `failure` is the
        message for the run's accounting.
    """
    destination = resolve_destination(output_root, target.destination)
    produced: list[Path] = []
    try:
        size = fetch(target.url, destination, session=session, max_bytes=max_bytes)
        produced = [destination]

        if target.expected_size is not None and size != target.expected_size:
            raise FetchError(
                f"{target.url} delivered {size:,} bytes, expected {target.expected_size:,}"
            )

        if post_process is not None:
            produced = [Path(path) for path in post_process(target, destination)]
            if not produced:
                raise ValueError(
                    f"Post-processing {target.url} produced no artifacts; a target that "
                    "resolves to nothing is a failure, not an empty success"
                )

        artifacts = [
            Artifact(path=_relative_to_root(path, output_root), size=path.stat().st_size)
            for path in produced
        ]
    # Any post-processor may raise anything; the boundary is deliberate, and
    # the failure is confined to this one target.
    except Exception as e:  # noqa: BLE001
        _delete_artifacts([*produced, destination])
        message = f"{target.url}: {e}"
        logger.error(f"Target failed: {message}")
        return (
            ManifestEntry(
                url=target.url,
                destination=target.destination,
                group=target.group,
                status=STATUS_FAILED,
                artifacts=[],
                error=str(e),
            ),
            message,
        )

    return (
        ManifestEntry(
            url=target.url,
            destination=target.destination,
            group=target.group,
            status=STATUS_FETCHED,
            artifacts=artifacts,
        ),
        None,
    )


def main(
    discover: Discoverer | None = None,
    post_process: PostProcessor | None = None,
) -> None:
    """Entry point: run one acquisition config and exit non-zero on any failure.

    Called with no arguments this is the fully generic ``acquire`` command: the
    discoverer and the post-processor are both built from the config, so a
    source whose targets can be computed needs no code. An instance whose
    targets must be scraped passes its own `discover` from a small wrapper.

    Args:
        discover: The source's discoverer, or None to build one from the
            config's ``[discovery]`` table.
        post_process: The source's post-processor, or None to build one from
            the config's ``[extract]`` table (which may itself be absent,
            meaning each target resolves to its own destination).

    Raises:
        SystemExit: With code 1 when the config is missing or invalid, the
            output root cannot be anchored, discovery fails its floor, or any
            target failed.
    """
    parser = build_parser(
        "Acquire a source's files: fetch every discovered target, record a "
        "manifest of what the run produced, and exit non-zero if any target failed.",
        env_file=False,
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    setup_entry_logging(LOG_SUBDIR, config_path)

    with run_scope():
        config = load_config(config_path)

        try:
            validate_config(config)
            resolved_discover = discover or build_discoverer(config)
            resolved_post_process = (
                post_process if post_process is not None else build_post_processor(config)
            )
            failures = run_acquisition(
                config,
                config_path,
                discover=resolved_discover,
                post_process=resolved_post_process,
            )
        except InstanceRootNotFoundError:
            # Already logged with the config path by require_instance_root.
            sys.exit(1)
        except ValueError as e:
            logger.error(f"Acquisition run failed: {e}")
            sys.exit(1)

        finish_run(
            failures,
            success_message=f"ACQUISITION PASSED: every target of {config_path.stem} acquired",
            failure_prefix="ACQUISITION FAILED",
        )


if __name__ == "__main__":
    main()
