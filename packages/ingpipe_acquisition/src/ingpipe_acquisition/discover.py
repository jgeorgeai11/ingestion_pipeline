"""Config-driven discoverers for sources whose targets can be COMPUTED.

Discovery splits by provenance, and the split is the reason this module is
small. A source whose URLs follow from a release point and a list of title
numbers *computes* its targets, so it needs no code: it declares a template and
a substitution list and disappears into config. A source that must fetch an
index, follow each page, and scrape a table for the folder name *finds* its
targets by traversal, and a config format expressive enough to describe that
would be a scraping DSL harder to read than the Python it replaced.

So this module ships the two computable shapes:

  - :func:`explicit_targets` -- an authored list of ``{url, destination}``.
  - :func:`templated_targets` -- one URL template and one destination template
    over a list of substitution values.

and documents the third:

  - **An instance-supplied callable.** Any callable ``(config) -> Iterable[Target]``
    is a discoverer. An instance passes its own to
    ``ingpipe_acquisition.runner.main(discover=...)`` from a wrapper of a few lines.
    That is the whole contract; ``policy_db``'s ``cms_iom`` discoverer is the
    reference implementation.

Both built-in discoverers are generators, and both validate every destination
as they yield it: a destination must be relative and must not contain a ``..``
component, because the runner resolves it under the output root and a config
typo should fail at the target, naming it, rather than at a path operation
somewhere later.
"""

from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

from ingpipe_lib.logconfig import get_logger
from ingpipe_lib.paths import is_rooted_path

from ingpipe_acquisition.manifest import Target

__all__ = [
    "build_discoverer",
    "explicit_targets",
    "templated_targets",
]

logger = get_logger(__name__)

# The discoverer kinds a config may name. An instance-supplied callable is not
# nameable here on purpose: passing code by config string would make the
# instance's own module a runtime lookup rather than an import.
DISCOVERY_KINDS = ("explicit", "templated")


def _validated_destination(raw: str, url: str) -> Path:
    """Validate a config-authored destination and return it as a path.

    Args:
        raw: The destination string from the config or template.
        url: The URL it belongs to, named in the error message.

    Returns:
        The destination as a relative :class:`~pathlib.Path`.

    Raises:
        ValueError: If the destination is empty, rooted, or contains a ``..``
            component. The runner re-checks the resolved path against the
            output root; this check exists so the failure names the offending
            target rather than a path. It is also the ONLY check on this
            value at discovery time, so it must not depend on the host.
    """
    if not raw:
        raise ValueError(f"Target for {url} has an empty destination")
    # Normalize separators once, before either check. Both clauses below are
    # otherwise host-dependent in the same way: PurePosixPath('..\\..\\x').parts
    # is a single component containing no "..", while the Windows rules split
    # the same string into four. Normalizing first is what makes the two
    # clauses agree with each other and give the same verdict on both hosts.
    normalized = raw.replace("\\", "/")
    # Judge the authored string, not Path(raw).is_absolute(): the latter asks
    # the HOST, so a POSIX-absolute destination passed this check untouched on
    # Windows, and a drive-relative one ("D:data") passes it on both.
    if is_rooted_path(normalized):
        raise ValueError(
            f"Target destination {raw!r} for {url} must be relative to the output root"
        )
    destination = Path(normalized)
    if ".." in destination.parts:
        raise ValueError(f"Target destination {raw!r} for {url} escapes the output root")
    return destination


def explicit_targets(config: dict) -> Iterator[Target]:
    """Yield the targets from a config-authored list.

    The shape for a source with a handful of stable URLs that follow no
    pattern worth templating::

        [discovery]
        kind = "explicit"
        targets = [
            { url = "https://example.gov/a.pdf", destination = "a.pdf" },
            { url = "https://example.gov/b.pdf", destination = "sub/b.pdf", group = "sub" },
        ]

    Args:
        config: The parsed TOML config.

    Yields:
        One :class:`~ingpipe_acquisition.manifest.Target` per authored entry.

    Raises:
        ValueError: If ``discovery.targets`` is missing or not a list of
            tables, if an entry lacks ``url`` or ``destination``, or if a
            destination is absolute or escaping.
    """
    discovery = _discovery_section(config)
    raw_targets = discovery.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ValueError(
            "Config key 'discovery.targets' must be a non-empty list of "
            "{ url, destination } tables for the explicit discoverer"
        )

    for index, raw in enumerate(raw_targets):
        if not isinstance(raw, dict):
            raise ValueError(f"discovery.targets[{index}] must be a table, got {raw!r}")
        url = raw.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError(f"discovery.targets[{index}] is missing a non-empty 'url'")
        destination = raw.get("destination")
        if not isinstance(destination, str):
            raise ValueError(f"discovery.targets[{index}] ({url}) is missing 'destination'")
        group = raw.get("group")
        if group is not None and not isinstance(group, str):
            raise ValueError(f"discovery.targets[{index}] ({url}) has a non-string 'group'")

        yield Target(
            url=url,
            destination=_validated_destination(destination, url),
            group=group,
        )


def templated_targets(config: dict) -> Iterator[Target]:
    """Yield the targets produced by substituting each value into two templates.

    The shape for a source whose URLs are a pattern over a known list -- the
    USC titles being the motivating case, where nine titles at one release
    point were previously nine lines of Python plus a URL builder::

        [discovery]
        kind = "templated"
        variable = "title"
        values = ["05", "10", "20"]
        url_template = "https://uscode.house.gov/.../pdf_usc{title}@119-73not60.zip"
        destination_template = "usc{title}/pdf_usc{title}.zip"
        group_template = "usc{title}"          # optional

    Only the single named ``variable`` is substituted. Anything else that
    varies -- a release point, a year -- is part of the template string, so a
    refresh is one edit in one place rather than a code change.

    Args:
        config: The parsed TOML config.

    Yields:
        One :class:`~ingpipe_acquisition.manifest.Target` per value.

    Raises:
        ValueError: If any of ``variable``, ``values``, ``url_template``, or
            ``destination_template`` is missing or the wrong type, if a
            template references a placeholder other than ``variable``, or if a
            resolved destination is absolute or escaping.
    """
    discovery = _discovery_section(config)

    variable = discovery.get("variable")
    if not isinstance(variable, str) or not variable:
        raise ValueError("Config key 'discovery.variable' must be a non-empty string")

    values = discovery.get("values")
    if not isinstance(values, list) or not values:
        raise ValueError("Config key 'discovery.values' must be a non-empty list")

    url_template = _required_template(discovery, "url_template")
    destination_template = _required_template(discovery, "destination_template")
    group_template = discovery.get("group_template")
    if group_template is not None and not isinstance(group_template, str):
        raise ValueError(
            f"Config key 'discovery.group_template' must be a string, got {group_template!r}"
        )

    for value in values:
        # TOML integers are convenient for title numbers but would defeat any
        # zero-padding the author expects, so values are used as written and
        # stringified only for substitution.
        substitution = {variable: value}
        url = _render(url_template, substitution, "discovery.url_template")
        destination = _render(
            destination_template, substitution, "discovery.destination_template"
        )
        group = (
            _render(group_template, substitution, "discovery.group_template")
            if group_template is not None
            else None
        )
        yield Target(
            url=url,
            destination=_validated_destination(destination, url),
            group=group,
        )


def _required_template(discovery: dict, key: str) -> str:
    """Read a required template string from the discovery section.

    Args:
        discovery: The ``[discovery]`` table.
        key: The template key to read.

    Returns:
        The template string.

    Raises:
        ValueError: If the key is missing or is not a non-empty string.
    """
    template = discovery.get(key)
    if not isinstance(template, str) or not template:
        raise ValueError(f"Config key 'discovery.{key}' must be a non-empty string")
    return template


def _render(template: str, substitution: dict[str, object], key: str) -> str:
    """Substitute the discovery variable into one template.

    Args:
        template: The template string.
        substitution: The single-entry mapping of variable name to value.
        key: The config key the template came from, named in errors.

    Returns:
        The rendered string.

    Raises:
        ValueError: If the template references an unknown placeholder or is
            malformed. Failing here means a typo in a template is a config
            error at load time rather than a wrong URL fetched at run time.
    """
    try:
        return template.format(**substitution)
    except (KeyError, IndexError, ValueError) as e:
        raise ValueError(
            f"Config key '{key}' ({template!r}) references an unknown placeholder; "
            f"only {{{next(iter(substitution))}}} is available: {e}"
        ) from e


def _discovery_section(config: dict) -> dict:
    """Return the config's ``[discovery]`` table.

    Args:
        config: The parsed TOML config.

    Returns:
        The discovery table.

    Raises:
        ValueError: If the table is missing or is not a table.
    """
    discovery = config.get("discovery")
    if not isinstance(discovery, dict):
        raise ValueError(
            "Config table '[discovery]' is required when no discoverer is supplied in code"
        )
    return discovery


def build_discoverer(config: dict) -> Callable[[dict], Iterable[Target]]:
    """Select the config-driven discoverer named by ``discovery.kind``.

    Args:
        config: The parsed TOML config.

    Returns:
        The discoverer callable for the configured kind.

    Raises:
        ValueError: If ``[discovery]`` is missing or names an unknown kind.
            An instance whose targets must be scraped does not name a kind at
            all -- it passes its callable to ``runner.main`` instead.
    """
    discovery = _discovery_section(config)
    kind = discovery.get("kind")
    if kind not in DISCOVERY_KINDS:
        raise ValueError(
            f"Config key 'discovery.kind' must be one of {list(DISCOVERY_KINDS)}, got {kind!r}. "
            "A source whose targets can only be found by scraping supplies a "
            "discover callable in code instead of naming a kind here."
        )
    logger.info(f"Using the {kind} discoverer")
    return explicit_targets if kind == "explicit" else templated_targets
