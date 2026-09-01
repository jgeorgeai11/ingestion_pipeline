"""Instance-root discovery and path anchoring for installed entry points.

Installed console scripts run from any working directory, so a config's
relative paths (``source_dir``, ``parsed_dir``, ``output_dir``, ...) can no
longer be resolved against the CWD: a run from the wrong directory would look
for ``~/data/input/...`` and either find nothing or scatter output beside the
caller. Instead, relative paths are anchored to the **instance root** — the
nearest ancestor of the ``--config`` file that contains a ``pyproject.toml``
(every instance is a uv workspace member, so the marker always exists).

Anchoring to the instance root rather than to the config file itself keeps
instance configs unedited: a config at
``instances/<instance>/config/ingpipe_file_ingestion/...`` can keep saying
``data/input/...`` and resolve to ``instances/<instance>/data/input/...``
regardless of where the command was invoked from.

Log directories are anchored the same way (``<instance>/logs/<subdir>``).
When a run has no instance around its config — or no config at all — logs
fall back to the documented location ``$TMPDIR/ingestion_pipeline/logs/``
rather than the working directory.
"""

import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from ingpipe_lib.logconfig import get_logger

__all__ = [
    "InstanceRootNotFoundError",
    "fallback_log_root",
    "find_instance_root",
    "is_rooted_path",
    "require_instance_root",
    "resolve_config_path",
    "resolve_log_dir",
]

logger = get_logger(__name__)

# The marker that identifies an instance root: every instance is a uv
# workspace member, so its root directory carries a pyproject.toml.
_INSTANCE_MARKER = "pyproject.toml"


class InstanceRootNotFoundError(Exception):
    """Raised when no instance root exists above a config file.

    A config with relative paths cannot be resolved without an anchor;
    failing loudly here replaces the old silent fallback to the working
    directory, which pointed a run at whatever the CWD happened to contain.
    """


def is_rooted_path(raw: str) -> bool:
    """Report whether ``raw`` carries a drive or a root under EITHER platform's rules.

    This is the workspace's replacement for ``Path.is_absolute()`` when judging
    an authored or remote-supplied path. ``Path`` follows the rules of the
    HOST, so the same string is accepted on one development machine and
    rejected on the other: ``/etc/passwd`` is absolute on POSIX but merely
    root-relative on Windows, and ``C:/data`` is absolute on Windows but an
    ordinary relative name on POSIX.

    Testing ``is_absolute()`` under BOTH rule sets is NOT sufficient, which is
    the trap this function exists to close. Three forms are absolute under
    neither rule set yet still escape a root they are joined under:

    * ``C:data`` and ``D:data`` (drive-relative) --
      ``PureWindowsPath('C:/root/out') / 'D:data'`` is ``D:data``, which has
      left the root entirely.
    * ``\\etc\\passwd`` (rooted but drive-less) --
      ``PureWindowsPath('C:/root/out') / '/etc/passwd'`` is ``C:\\etc\\passwd``.

    What a caller actually needs to know is "is this safe to join under a
    root?", so the test is for a drive or a root rather than for absoluteness,
    and the name says ``rooted`` rather than ``absolute`` -- "absolute" is the
    word that produced the original defect.

    Args:
        raw: The path as authored or as received, in string form. A caller
            holding a :class:`~pathlib.Path` must pass ``path.as_posix()``,
            never ``str(path)``: ``str()`` on a Windows ``Path`` renders
            ``/etc/passwd`` as ``\\etc\\passwd``, so the separator the check
            sees would depend on the host all over again.

    Returns:
        True when `raw` carries a drive or a root under Windows or POSIX
        rules, and is therefore unsafe to join under a root; False for a
        genuinely relative value such as ``data/in`` or ``../data/in`` (note
        that ``..`` escapes are a separate concern this predicate does not
        judge).
    """
    windows = PureWindowsPath(raw)
    posix = PurePosixPath(raw)
    return bool(windows.drive or windows.root or posix.root)


def find_instance_root(start: str | Path) -> Path | None:
    """Walk up from ``start`` to the nearest directory containing a ``pyproject.toml``.

    Args:
        start: A file or directory to start the walk from (typically the
            ``--config`` path). The path is resolved first, so a relative
            CWD-based path works.

    Returns:
        The nearest ancestor directory (including ``start`` itself when it is
        a directory) that contains a ``pyproject.toml``, or None when the walk
        reaches the filesystem root without finding one.
    """
    start_path = Path(start).resolve()
    candidates = [start_path, *start_path.parents] if start_path.is_dir() else start_path.parents
    for candidate in candidates:
        if (candidate / _INSTANCE_MARKER).is_file():
            return candidate
    return None


def require_instance_root(config_path: str | Path) -> Path:
    """Return the instance root above ``config_path``, or fail actionably.

    Args:
        config_path: The ``--config`` path whose instance the caller needs.

    Returns:
        The instance root directory.

    Raises:
        InstanceRootNotFoundError: If no ancestor of `config_path` contains a
            ``pyproject.toml``. The message names the config path so the
            operator can see which invocation was unanchored.
    """
    root = find_instance_root(config_path)
    if root is None:
        message = (
            f"No instance root found above config {config_path!s}: relative "
            "paths in a config are resolved against the nearest ancestor "
            "directory containing a pyproject.toml. Move the config under an "
            "instance (e.g. instances/<name>/config/...) or use absolute paths."
        )
        logger.error(message)
        raise InstanceRootNotFoundError(message)
    return root


def resolve_config_path(raw: str | Path, config_path: str | Path) -> Path:
    """Resolve a config-authored filesystem path against the config's instance root.

    A config path is either host-absolute or relative to the instance root.
    A value that is rooted only under the OTHER platform's rules is neither,
    and is rejected rather than quietly reinterpreted: joining ``/data/in``
    under a Windows instance root yields a path on whichever drive happens to
    be current, so the same config would mean different things on the two
    development machines. Rejecting only that ambiguous form leaves both
    working branches untouched -- a genuinely host-absolute path still passes
    through as an escape hatch for a corpus parked on another volume.

    Args:
        raw: The path string as written in the TOML config. An absolute path
            is returned unchanged; a relative one is anchored at the instance
            root of `config_path`.
        config_path: The ``--config`` path the value came from, used to
            discover the instance root.

    Returns:
        The resolved absolute path.

    Raises:
        ValueError: If `raw` is rooted under the other platform's rules, and
            so has no host-independent meaning.
        InstanceRootNotFoundError: If `raw` is relative and no instance root
            exists above `config_path`.
    """
    raw_path = Path(raw)
    if raw_path.is_absolute():
        return raw_path
    # Judge the normalized POSIX rendering, not str(raw_path) (whose
    # separators follow the host) and not the caller's argument (which may be
    # either a str or a Path).
    as_authored = raw_path.as_posix()
    if is_rooted_path(as_authored):
        message = (
            f"Config path {raw!s} is rooted under the other platform's rules but not "
            "this host's, so it has no single meaning across the supported platforms. "
            "A config path is either absolute on this host or relative to the instance "
            "root; write it relative to the instance root (e.g. data/input/...)."
        )
        logger.error(message)
        raise ValueError(message)
    resolved = require_instance_root(config_path) / raw_path
    logger.debug(f"Resolved config path {raw!s} -> {resolved}")
    return resolved


def fallback_log_root() -> Path:
    """Return the documented log location for runs with no instance.

    Engine unit tests and config-less invocations have no instance root to
    anchor to; their logs collect under the system temp directory rather
    than the working directory, so an installed script never litters the
    caller's CWD.

    Returns:
        ``$TMPDIR/ingestion_pipeline/logs``.
    """
    return Path(tempfile.gettempdir()) / "ingestion_pipeline" / "logs"


def resolve_log_dir(subdir: str | Path, config_path: str | Path | None = None) -> Path:
    """Resolve the log directory for an entry-point run.

    Args:
        subdir: The per-module log subdirectory (e.g. ``"ingpipe_file_ingestion"``).
        config_path: The run's ``--config`` path, when it has one. When an
            instance root exists above it, logs land in
            ``<instance>/logs/<subdir>``; otherwise (or when None) they land
            in the fallback location (see :func:`fallback_log_root`).

    Returns:
        The directory to hand to ``setup_logging(log_dir=...)``.
    """
    if config_path is not None:
        root = find_instance_root(config_path)
        if root is not None:
            return root / "logs" / subdir
    return fallback_log_root() / subdir
