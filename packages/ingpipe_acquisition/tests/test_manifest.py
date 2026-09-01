"""Tests for ingpipe_acquisition.manifest.

The manifest is the load-bearing artifact of the acquisition package: skip
decisions and validation both read it, so a round-trip that loses a field or a
malformed file that parses as "empty" would silently defeat both. These tests
pin the round trip, the hard-failure behavior on absence and corruption, and
the relative-path storage that lets an instance directory move.
"""

import json
import os
from pathlib import Path

import pytest
from ingpipe_acquisition.manifest import (
    MANIFEST_FILENAME,
    STATUS_FAILED,
    STATUS_SKIPPED,
    Artifact,
    ManifestEntry,
    ManifestError,
    index_by_url,
    manifest_path,
    read_manifest,
    write_manifest,
)


def _entry(url: str = "https://example.test/a.pdf", **overrides) -> ManifestEntry:
    """Build a manifest entry with sensible defaults for the tests."""
    defaults = {
        "url": url,
        "destination": Path("group_a/a.pdf"),
        "group": "group_a",
        "artifacts": [Artifact(path=Path("group_a/a.pdf"), size=1024)],
    }
    defaults.update(overrides)
    return ManifestEntry(**defaults)  # type: ignore[arg-type]


def test_write_manifest_round_trips_through_read_manifest(tmp_path):
    """A written manifest reads back with every field unchanged."""
    entries = [
        _entry("https://example.test/a.pdf"),
        _entry(
            "https://example.test/b.zip",
            destination=Path("usc05/b.zip"),
            group="usc05",
            status=STATUS_SKIPPED,
            artifacts=[
                Artifact(path=Path("usc05/one.pdf"), size=7),
                Artifact(path=Path("usc05/two.pdf"), size=9),
            ],
        ),
        _entry(
            "https://example.test/c.pdf",
            status=STATUS_FAILED,
            artifacts=[],
            error="HTTP 404",
        ),
    ]

    write_manifest(tmp_path, entries)
    restored = read_manifest(tmp_path)

    assert restored == entries


def test_write_manifest_creates_the_output_root_when_absent(tmp_path):
    """The manifest write creates its own directory rather than failing."""
    root = tmp_path / "not_yet_created"

    written = write_manifest(root, [_entry()])

    assert written == root / MANIFEST_FILENAME
    assert written.is_file()


def test_write_manifest_leaves_no_partial_file(tmp_path):
    """The atomic write leaves no ``.part`` residue behind."""
    write_manifest(tmp_path, [_entry()])

    assert not (tmp_path / (MANIFEST_FILENAME + ".part")).exists()


def test_write_manifest_records_a_generated_at_timestamp(tmp_path):
    """Each run stamps the manifest so an operator can date the corpus."""
    write_manifest(tmp_path, [_entry()])

    payload = json.loads(manifest_path(tmp_path).read_text(encoding="utf-8"))

    assert payload["generated_at"].endswith("+00:00")


def test_read_manifest_missing_file_raises_manifest_error(tmp_path):
    """A missing manifest is a hard failure, never an empty pass."""
    with pytest.raises(ManifestError, match="No acquisition manifest"):
        read_manifest(tmp_path)


def test_read_manifest_malformed_json_raises_manifest_error(tmp_path):
    """Unparseable JSON fails loudly instead of degrading to zero entries."""
    manifest_path(tmp_path).write_text("{not json", encoding="utf-8")

    with pytest.raises(ManifestError, match="Malformed acquisition manifest"):
        read_manifest(tmp_path)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("[]", "top level is not an object"),
        ('{"entries": {}}', "'entries' is not a list"),
        ('{"entries": ["a string"]}', "expected an object"),
        ('{"entries": [{"destination": "a.pdf"}]}', "missing or non-string 'url'"),
        ('{"entries": [{"url": "u"}]}', "missing 'destination'"),
        ('{"entries": [{"url": "u", "destination": "d", "status": "maybe"}]}', "unknown status"),
        ('{"entries": [{"url": "u", "destination": "d", "group": 7}]}', "non-string 'group'"),
        ('{"entries": [{"url": "u", "destination": "d", "error": 7}]}', "non-string 'error'"),
        ('{"entries": [{"url": "u", "destination": "d", "artifacts": 3}]}', "non-list 'artifacts'"),
        ('{"entries": [{"url": "u", "destination": "d", "artifacts": [1]}]}', "is not an object"),
        (
            '{"entries": [{"url": "u", "destination": "d", "artifacts": [{"size": 1}]}]}',
            "missing 'path'",
        ),
        (
            '{"entries": [{"url": "u", "destination": "d",'
            ' "artifacts": [{"path": "p", "size": "big"}]}]}',
            "non-integer 'size'",
        ),
        (
            '{"entries": [{"url": "u", "destination": "d",'
            ' "artifacts": [{"path": "p", "size": true}]}]}',
            "non-integer 'size'",
        ),
        (
            '{"entries": [{"url": "u", "destination": "d",'
            ' "artifacts": [{"path": "p", "size": -1}]}]}',
            "non-integer 'size'",
        ),
    ],
)
def test_read_manifest_structural_defects_raise_manifest_error(tmp_path, payload, expected):
    """Every structural deviation is fatal, naming the offending record."""
    manifest_path(tmp_path).write_text(payload, encoding="utf-8")

    with pytest.raises(ManifestError, match=expected):
        read_manifest(tmp_path)


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows ignores POSIX mode bits, so chmod(0o000) leaves the file "
    "readable and the unreadable-file branch cannot be provoked this way. "
    "The branch keeps its coverage on macOS; simulating the failure by "
    "patching would test the mock rather than the platform.",
)
def test_read_manifest_unreadable_file_raises_manifest_error(tmp_path):
    """An I/O failure surfaces as ManifestError, not a bare OSError."""
    # A directory at the manifest's name is a file that exists for is_file()
    # purposes only if it is a regular file -- so make a real file unreadable.
    target = manifest_path(tmp_path)
    target.write_text('{"entries": []}', encoding="utf-8")
    target.chmod(0o000)

    try:
        with pytest.raises(ManifestError, match="Malformed acquisition manifest"):
            read_manifest(tmp_path)
    finally:
        target.chmod(0o600)


def test_write_manifest_failure_raises_manifest_error(tmp_path, mocker):
    """A failed write raises the typed error rather than a bare OSError."""
    mocker.patch("ingpipe_acquisition.manifest.open", side_effect=OSError("disk full"))

    with pytest.raises(ManifestError, match="Failed to write manifest"):
        write_manifest(tmp_path, [_entry()])


def test_destinations_are_stored_relative_to_the_output_root(tmp_path):
    """Stored paths are relative, so a manifest survives a directory move."""
    write_manifest(tmp_path, [_entry()])

    payload = json.loads(manifest_path(tmp_path).read_text(encoding="utf-8"))
    stored = payload["entries"][0]

    assert stored["destination"] == "group_a/a.pdf"
    assert stored["artifacts"][0]["path"] == "group_a/a.pdf"
    assert str(tmp_path) not in manifest_path(tmp_path).read_text(encoding="utf-8")


def test_manifest_stays_valid_after_the_output_root_moves(tmp_path):
    """Reading a manifest from a moved directory yields the same entries."""
    original = tmp_path / "before"
    original.mkdir()
    write_manifest(original, [_entry()])

    moved = tmp_path / "after"
    original.rename(moved)

    assert read_manifest(moved) == [_entry()]


def test_index_by_url_keys_entries_by_source_url(tmp_path):
    """The skip index is keyed on the URL, with last write winning."""
    first = _entry("https://example.test/a.pdf")
    duplicate = _entry("https://example.test/a.pdf", destination=Path("group_b/a.pdf"))

    index = index_by_url([first, duplicate])

    assert index == {"https://example.test/a.pdf": duplicate}
