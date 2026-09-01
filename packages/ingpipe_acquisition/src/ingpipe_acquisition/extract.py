"""The built-in extraction post-processor.

Sources that ship their documents inside an archive (USC serves one zip per
title) need the archive turned into corpus files before anything downstream
can see them. That work is implemented *through* the runner's ``post_process``
hook rather than beside it, deliberately: the hook is the escape route for a
source whose arrival handling does not fit ``keep``/``flatten``, and a hook
with no real user is a dormant branch that rots. Building the package's own
extraction on it means the contract is exercised by every USC run.

Two behaviors here are corrections of the old USC script:

  - It called ``zf.extractall`` and then pruned, so a zip full of unwanted
    formats was fully written to disk first. :func:`make_extractor` extracts
    only the members matching ``keep``.
  - It reported success for an archive that yielded nothing usable. An archive
    with no matching member raises, so the target fails and the next run
    retries it.
"""

import fnmatch
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path

from ingpipe_lib.logconfig import get_logger
from ingpipe_lib.paths import is_rooted_path

from ingpipe_acquisition.manifest import Target

__all__ = ["ExtractionError", "build_post_processor", "make_extractor"]

logger = get_logger(__name__)


class ExtractionError(Exception):
    """Raised when an archive cannot be turned into usable corpus files.

    Typed so a wrong, empty, or corrupt archive fails exactly one target with
    a message naming it, rather than surfacing as a bare ``BadZipFile`` from
    somewhere inside the run loop.
    """


def _matches(name: str, patterns: Sequence[str] | None) -> bool:
    """Check a zip member name against the ``keep`` glob patterns.

    Both the full member path and its basename are tested, so ``"*.pdf"``
    matches ``"usc05/title05.pdf"`` without the config having to know the
    archive's internal directory layout.

    Args:
        name: The member name as stored in the archive.
        patterns: The glob patterns to keep, or None to keep everything.

    Returns:
        True when the member should be extracted.
    """
    if not patterns:
        return True
    basename = name.rsplit("/", 1)[-1]
    return any(
        fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(basename, pattern)
        for pattern in patterns
    )


def _member_destination(name: str, extract_dir: Path, *, flatten: bool) -> Path:
    """Resolve where one archive member is written, rejecting zip-slip.

    Args:
        name: The member name as stored in the archive.
        extract_dir: The directory members are written under.
        flatten: When True the member's directory components are discarded and
            only its basename is used.

    Returns:
        The absolute path to write the member to.

    Raises:
        ExtractionError: If the member's path escapes `extract_dir` -- an
            archive is remote content, so ``../../etc/x`` is a real risk.
    """
    # Normalize separators on ENTRY, before the flatten split. An archive
    # built on Windows carries backslash-separated member names, and this
    # guard sees untrusted remote content, so neither clause below may depend
    # on the host: PurePosixPath('..\\..\\etc/passwd').parts contains no ".."
    # at all. Normalizing AFTER the split would be wrong rather than merely
    # late -- the split is on "/" only, so "a\b\evil.pdf" would reach flatten
    # as one long filename and only then be split, changing what flatten
    # means instead of what this guard catches.
    normalized = name.replace("\\", "/")
    relative = Path(normalized.rsplit("/", 1)[-1]) if flatten else Path(normalized)
    # as_posix() rather than is_absolute(): the latter asks the host, so a
    # "/escape.pdf" member was rooted on POSIX and an ordinary relative name
    # on Windows.
    if is_rooted_path(relative.as_posix()) or ".." in relative.parts:
        raise ExtractionError(f"Archive member {name!r} escapes the extraction directory")

    root = extract_dir.resolve()
    candidate = (root / relative).resolve()
    if root not in candidate.parents:
        raise ExtractionError(f"Archive member {name!r} escapes the extraction directory")
    return candidate


def make_extractor(
    keep: Sequence[str] | None = None,
    *,
    flatten: bool = False,
    delete_archive: bool = True,
) -> Callable[[Target, Path], list[Path]]:
    """Build a post-processor that extracts an archive into its own directory.

    Members are written under the archive's parent directory, which is what
    makes a destination of ``usc05/pdf_usc05@119-73not60.zip`` produce the
    PDFs the rest of the pipeline expects at ``usc05/``.

    Args:
        keep: Glob patterns naming the members to extract (e.g.
            ``["*.pdf"]``). None extracts every member. Only matching members
            are written -- the archive is never fully unpacked and then pruned.
        flatten: When True, member directory components are discarded so every
            kept file lands directly in the extraction directory. Defaults to
            False.
        delete_archive: When True the archive is removed after a successful
            extraction, since an archive is not corpus content and would
            otherwise be ingested or validated as though it were. Defaults to
            True.

    Returns:
        A callable matching :data:`ingpipe_acquisition.runner.PostProcessor`: it takes
        the target and the downloaded archive path and returns the extracted
        artifact paths.
    """

    def extract(target: Target, archive: Path) -> list[Path]:
        """Extract `archive`, returning the artifacts the target resolved to.

        Args:
            target: The target the archive arrived for, used in messages.
            archive: The downloaded archive.

        Returns:
            The extracted file paths, sorted for a deterministic manifest.

        Raises:
            ExtractionError: If the archive is not a readable zip, if no
                member matches `keep`, or if a member's path escapes the
                extraction directory. Any partially extracted files are
                removed first, so a failed target leaves no residue.
        """
        extract_dir = archive.parent
        written: list[Path] = []
        try:
            with zipfile.ZipFile(archive) as zf:
                members = [info for info in zf.infolist() if not info.is_dir()]
                kept = [info for info in members if _matches(info.filename, keep)]
                logger.info(
                    f"Extracting {len(kept)} of {len(members)} member(s) from "
                    f"{archive.name} -> {extract_dir}"
                )
                if not kept:
                    raise ExtractionError(
                        f"Archive {archive.name} for {target.url} contains no member "
                        f"matching keep={list(keep) if keep else ['*']}; an archive that "
                        "yields nothing is a failed target, not an empty success"
                    )

                for info in kept:
                    destination = _member_destination(info.filename, extract_dir, flatten=flatten)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as source, open(destination, "wb") as handle:
                        handle.write(source.read())
                    written.append(destination)
        except (zipfile.BadZipFile, OSError) as e:
            _remove_all(written)
            logger.error(f"Failed to extract {archive}: {e}")
            raise ExtractionError(f"Failed to extract {archive.name}: {e}") from e
        except ExtractionError:
            _remove_all(written)
            raise

        if delete_archive:
            archive.unlink(missing_ok=True)
            logger.debug(f"Deleted archive after extraction: {archive.name}")

        return sorted(written)

    return extract


def _remove_all(paths: Sequence[Path]) -> None:
    """Delete a partial extraction so a failed target leaves nothing behind.

    Args:
        paths: The files written before the failure.
    """
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning(f"Could not remove partially extracted {path}: {e}")


def build_post_processor(config: dict) -> Callable[[Target, Path], list[Path]] | None:
    """Build the run's post-processor from its ``[extract]`` table, if any.

    A source with no ``[extract]`` table (or with ``enabled = false``) gets
    None, meaning every target resolves to its own destination.

    Args:
        config: The parsed TOML config.

    Returns:
        An extractor callable, or None when the config asks for no
        post-processing.

    Raises:
        ValueError: If ``[extract]`` is present but malformed. Config errors
            are raised as ``ValueError`` so the entry point has one abort path.
    """
    section = config.get("extract")
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ValueError(f"Config table '[extract]' must be a table, got {section!r}")

    enabled = section.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"Config key 'extract.enabled' must be a boolean, got {enabled!r}")
    if not enabled:
        return None

    keep = section.get("keep")
    if keep is not None and (
        not isinstance(keep, list) or not all(isinstance(pattern, str) for pattern in keep)
    ):
        raise ValueError(f"Config key 'extract.keep' must be a list of strings, got {keep!r}")

    flatten = section.get("flatten", False)
    delete_archive = section.get("delete_archive", True)
    for key, value in (("flatten", flatten), ("delete_archive", delete_archive)):
        if not isinstance(value, bool):
            raise ValueError(f"Config key 'extract.{key}' must be a boolean, got {value!r}")

    logger.info(
        f"Post-processing: extract keep={keep or ['*']}, flatten={flatten}, "
        f"delete_archive={delete_archive}"
    )
    return make_extractor(keep, flatten=flatten, delete_archive=delete_archive)
