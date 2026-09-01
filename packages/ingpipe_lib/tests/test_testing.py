"""Unit tests for ingpipe_lib.testing's non-fixture helpers.

Only ``assert_example_config_valid`` is covered here. It is the assertion the
five stage packages point at their shipped ``config/example.toml`` files, so a
defect in it would quietly disarm every one of those guards at once — the exact
failure mode the guards exist to prevent, one level up.

Every test builds a THROWAWAY package on ``sys.path`` rather than pointing at a
real engine package. Pointing at a real one would couple this file to whichever
example is currently correct, so the helper's own tests would fail whenever an
engine example is the thing that is broken — and the broken example is what the
suite should be reporting, not this.

Nothing here touches a database: the helper takes no fixture, reads one packaged
file, and calls the validator it is handed.
"""

import sys
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from ingpipe_lib.testing import assert_example_config_valid

# A minimal but not trivial example: the nested table proves the helper hands
# the validator a fully parsed config rather than a flat top level.
VALID_EXAMPLE = """
output_dir = "data/input/example_source"
min_targets = 3

[http]
retries = 3
"""


@pytest.fixture
def throwaway_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[str | None], str]]:
    """Yield a factory that builds importable throwaway packages on ``sys.path``.

    The factory takes the text to write to ``config/example.toml``, or None to
    build a package that ships no example at all, and returns the package's
    import name. Each package gets a UUID-derived name so no test can be served
    a package another test already imported and left in ``sys.modules``.

    Args:
        tmp_path: Per-test temporary directory, prepended to ``sys.path``.
        monkeypatch: Used to restore ``sys.path`` in teardown.

    Yields:
        Callable taking the example's TOML text (or None) and returning the
        throwaway package's import name.
    """
    monkeypatch.syspath_prepend(str(tmp_path))
    created: list[str] = []

    def make(example_toml: str | None) -> str:
        name = f"throwaway_pkg_{uuid.uuid4().hex[:12]}"
        package_dir = tmp_path / name
        package_dir.mkdir()
        (package_dir / "__init__.py").write_text("", encoding="utf-8")
        if example_toml is not None:
            config_dir = package_dir / "config"
            config_dir.mkdir()
            (config_dir / "example.toml").write_text(example_toml, encoding="utf-8")
        created.append(name)
        return name

    yield make

    # The packages themselves go with tmp_path, but their sys.modules entries
    # would outlive the test and keep the (deleted) directories importable.
    for name in created:
        sys.modules.pop(name, None)


def test_a_valid_example_returns_the_parsed_config(
    throwaway_package: Callable[[str | None], str],
) -> None:
    """An example its validator accepts comes back parsed, not as text or None.

    Returning the parsed dict is what lets a caller assert on specific keys
    beyond "it validates" — the mirror's per-table checks are the live case.
    """
    # Arrange: a validator that accepts everything and records what it saw, so
    # the test can prove the helper actually called it with the parsed config.
    package = throwaway_package(VALID_EXAMPLE)
    seen: list[dict] = []

    # Act
    config = assert_example_config_valid(package, seen.append)

    # Assert
    assert config == {
        "output_dir": "data/input/example_source",
        "min_targets": 3,
        "http": {"retries": 3},
    }
    assert seen == [config]


def test_a_rejecting_validator_propagates_its_own_message(
    throwaway_package: Callable[[str | None], str],
) -> None:
    """A rejected example fails with the VALIDATOR's message, not a generic one.

    The validators carry deliberately specific messages naming the offending
    key; swallowing one and re-raising "example config is invalid" would throw
    away the only part of the failure that tells a maintainer what to fix.
    """
    # Arrange
    package = throwaway_package(VALID_EXAMPLE)

    def reject(config: dict) -> None:
        raise ValueError("Config key 'min_targets' must be an integer >= 1, got 3")

    # Act / Assert
    with pytest.raises(ValueError, match=r"Config key 'min_targets' must be an integer >= 1"):
        assert_example_config_valid(package, reject)


def test_a_package_without_an_example_fails_naming_the_package(
    throwaway_package: Callable[[str | None], str],
) -> None:
    """A package that stops shipping its example is diagnosable, not mysterious.

    Without the package name in the message, this failure reads as a bare
    FileNotFoundError from inside a library, and the reader has to work out
    which of the five callers raised it.
    """
    # Arrange
    package = throwaway_package(None)

    # Act / Assert
    with pytest.raises(AssertionError, match=package):
        assert_example_config_valid(package, lambda config: None)
