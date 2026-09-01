"""Tests for ingpipe_acquisition.data_validation.data_val_downloads.

The point of this validator is to be able to detect INCOMPLETENESS, which the
two validators it replaces could not: one passed a folder holding any single
PDF, the other asserted the presence of archives the downloader deletes. The
tests below therefore concentrate on the failures that used to pass -- a
missing artifact, a truncated one, a manifest that records a failed target,
and a corpus with no manifest at all.
"""

import os
import zipfile
from pathlib import Path

import pytest
from ingpipe_acquisition.data_validation.data_val_downloads import (
    main,
    validate_content_checks,
    validate_downloads,
)
from ingpipe_acquisition.manifest import (
    STATUS_FAILED,
    STATUS_SKIPPED,
    Artifact,
    ManifestEntry,
    ManifestError,
    manifest_path,
    write_manifest,
)


@pytest.fixture
def instance(tmp_path):
    """Build a minimal instance root (a directory with a pyproject.toml)."""
    root = tmp_path / "instance"
    (root / "config").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='t'\n", encoding="utf-8")
    return root


@pytest.fixture
def config_path(instance):
    """Return the validator config path inside the instance."""
    path = instance / "config" / "data_val_downloads.toml"
    path.write_text('output_dir = "data/input/source"\n', encoding="utf-8")
    return path


@pytest.fixture
def output_root(instance):
    """Return (and create) the corpus root the config points at."""
    root = instance / "data" / "input" / "source"
    root.mkdir(parents=True)
    return root


def _config(**overrides) -> dict:
    """Build a validator config."""
    config = {"output_dir": "data/input/source"}
    config.update(overrides)
    return config


def _place(output_root: Path, relative: str, payload: bytes) -> Artifact:
    """Write a corpus file and return the artifact record describing it."""
    path = output_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return Artifact(path=Path(relative), size=len(payload))


def _entry(url: str, artifacts: list[Artifact], **overrides) -> ManifestEntry:
    """Build a manifest entry for the given artifacts."""
    defaults = {
        "url": url,
        "destination": artifacts[0].path if artifacts else Path("missing"),
        "artifacts": artifacts,
    }
    defaults.update(overrides)
    return ManifestEntry(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_a_corpus_matching_its_manifest_passes(config_path, output_root):
    """Every recorded artifact present at its recorded size is a clean run."""
    artifacts = [
        _place(output_root, "usc05/a.pdf", b"%PDF-1.7 body"),
        _place(output_root, "usc05/b.pdf", b"%PDF-1.7 other"),
    ]
    write_manifest(output_root, [_entry("https://x.test/t.zip", artifacts)])

    assert validate_downloads(_config(), config_path) == []


def test_a_skipped_entry_is_validated_like_a_fetched_one(config_path, output_root):
    """A resume's skipped targets are checked, not trusted."""
    artifact = _place(output_root, "a.pdf", b"%PDF body")
    write_manifest(
        output_root, [_entry("https://x.test/a.pdf", [artifact], status=STATUS_SKIPPED)]
    )

    assert validate_downloads(_config(), config_path) == []

    (output_root / "a.pdf").unlink()

    assert len(validate_downloads(_config(), config_path)) == 1


# ---------------------------------------------------------------------------
# The failures that used to pass
# ---------------------------------------------------------------------------


def test_a_missing_artifact_fails(config_path, output_root):
    """The check the old per-folder validator could not make: 1 of 40."""
    present = _place(output_root, "manual/a.pdf", b"%PDF one")
    absent = Artifact(path=Path("manual/b.pdf"), size=42)
    write_manifest(output_root, [_entry("https://x.test/m", [present, absent])])

    failures = validate_downloads(_config(), config_path)

    assert len(failures) == 1
    assert "manual/b.pdf: missing" in failures[0]


def test_a_zero_byte_artifact_fails(config_path, output_root):
    """An empty file is reported as empty rather than as a size mismatch."""
    _place(output_root, "a.pdf", b"")
    write_manifest(
        output_root,
        [_entry("https://x.test/a.pdf", [Artifact(path=Path("a.pdf"), size=0)])],
    )

    failures = validate_downloads(_config(), config_path)

    assert len(failures) == 1
    assert "is empty" in failures[0]


def test_a_size_mismatch_fails(config_path, output_root):
    """A truncated file no longer satisfies a validator that only rejects zero."""
    _place(output_root, "a.pdf", b"%PDF truncated")
    write_manifest(
        output_root,
        [_entry("https://x.test/a.pdf", [Artifact(path=Path("a.pdf"), size=99_999)])],
    )

    failures = validate_downloads(_config(), config_path)

    assert len(failures) == 1
    assert "the run recorded 99,999" in failures[0]


def test_a_failed_entry_fails_validation_even_when_every_file_is_correct(
    config_path, output_root
):
    """Regression guard for the resume hole.

    A run that fetched 37 of 40 targets writes a 40-entry manifest with three
    failures. Every file that IS present is correct, so a validator that only
    walked the disk would pass -- and would be blind to exactly the three
    documents the corpus is missing.
    """
    good = _place(output_root, "a.pdf", b"%PDF good")
    write_manifest(
        output_root,
        [
            _entry("https://x.test/a.pdf", [good]),
            _entry(
                "https://x.test/b.pdf",
                [],
                status=STATUS_FAILED,
                destination=Path("b.pdf"),
                error="HTTP 503",
            ),
        ],
    )

    failures = validate_downloads(_config(), config_path)

    assert len(failures) == 1
    assert "recorded as FAILED" in failures[0]
    assert "HTTP 503" in failures[0]


def test_an_entry_with_no_artifacts_fails(config_path, output_root):
    """A successful entry that recorded nothing verifies nothing."""
    write_manifest(
        output_root,
        [_entry("https://x.test/a.pdf", [], destination=Path("a.pdf"))],
    )

    failures = validate_downloads(_config(), config_path)

    assert len(failures) == 1
    assert "recorded no artifacts" in failures[0]


def test_all_failures_are_accumulated_not_short_circuited(config_path, output_root):
    """Every problem is reported in one run, so one pass fixes them all."""
    write_manifest(
        output_root,
        [
            _entry("https://x.test/a", [Artifact(path=Path("a.pdf"), size=1)]),
            _entry("https://x.test/b", [Artifact(path=Path("b.pdf"), size=1)]),
            _entry("https://x.test/c", [Artifact(path=Path("c.pdf"), size=1)]),
        ],
    )

    assert len(validate_downloads(_config(), config_path)) == 3


# ---------------------------------------------------------------------------
# The manifest itself
# ---------------------------------------------------------------------------


def test_a_missing_manifest_fails_rather_than_passing(config_path, output_root):
    """A corpus nothing claims to have produced cannot be reported as valid."""
    with pytest.raises(ManifestError, match="No acquisition manifest"):
        validate_downloads(_config(), config_path)


def test_a_malformed_manifest_fails(config_path, output_root):
    """An unparseable manifest is a failure, not zero entries to check."""
    manifest_path(output_root).write_text("{not json", encoding="utf-8")

    with pytest.raises(ManifestError, match="Malformed acquisition manifest"):
        validate_downloads(_config(), config_path)


def test_a_missing_output_dir_is_a_config_error(config_path):
    """The one required key is checked before anything is read."""
    with pytest.raises(ValueError, match="'output_dir' is required"):
        validate_downloads({}, config_path)


def test_an_unanchored_relative_output_dir_fails(tmp_path):
    """A relative output_dir with no instance root above it fails loudly."""
    from ingpipe_lib.paths import InstanceRootNotFoundError

    orphan = tmp_path / "orphan.toml"
    orphan.write_text("", encoding="utf-8")

    with pytest.raises(InstanceRootNotFoundError):
        validate_downloads(_config(), orphan)


# ---------------------------------------------------------------------------
# Content checks
# ---------------------------------------------------------------------------


def test_the_pdf_content_check_accepts_a_valid_pdf(config_path, output_root):
    """A file whose magic bytes are right passes the per-extension check."""
    artifact = _place(output_root, "a.pdf", b"%PDF-1.7 and the rest")
    write_manifest(output_root, [_entry("https://x.test/a.pdf", [artifact])])

    assert validate_downloads(_config(content_checks={".pdf": "pdf"}), config_path) == []


def test_the_pdf_content_check_rejects_the_wrong_magic_bytes(config_path, output_root):
    """An HTML error page saved as a PDF is caught by the header check."""
    artifact = _place(output_root, "a.pdf", b"<html>404 Not Found</html>")
    write_manifest(output_root, [_entry("https://x.test/a.pdf", [artifact])])

    failures = validate_downloads(_config(content_checks={".pdf": "pdf"}), config_path)

    assert len(failures) == 1
    assert "does not begin with %PDF" in failures[0]


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows ignores POSIX mode bits, so chmod(0o000) leaves the file "
    "readable and the unreadable-file branch cannot be provoked this way. "
    "The branch keeps its coverage on macOS; simulating the failure by "
    "patching would test the mock rather than the platform.",
)
def test_the_pdf_content_check_reports_an_unreadable_file(config_path, output_root):
    """A file that cannot be read fails its check rather than crashing."""
    artifact = _place(output_root, "a.pdf", b"%PDF body")
    write_manifest(output_root, [_entry("https://x.test/a.pdf", [artifact])])
    (output_root / "a.pdf").chmod(0o000)

    try:
        failures = validate_downloads(_config(content_checks={".pdf": "pdf"}), config_path)
    finally:
        (output_root / "a.pdf").chmod(0o600)

    assert len(failures) == 1
    assert "could not be read" in failures[0]


def test_the_zip_content_check_accepts_a_readable_archive(config_path, output_root):
    """Reading the central directory is a real completeness check."""
    path = output_root / "a.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("member.txt", b"content")
    artifact = Artifact(path=Path("a.zip"), size=path.stat().st_size)
    write_manifest(output_root, [_entry("https://x.test/a.zip", [artifact])])

    assert validate_downloads(_config(content_checks={".zip": "zip"}), config_path) == []


def test_the_zip_content_check_rejects_a_corrupt_archive(config_path, output_root):
    """A truncated archive fails even though its size is whatever it is."""
    artifact = _place(output_root, "a.zip", b"PK\x03\x04 truncated right here")
    write_manifest(output_root, [_entry("https://x.test/a.zip", [artifact])])

    failures = validate_downloads(_config(content_checks={".zip": "zip"}), config_path)

    assert len(failures) == 1
    assert "not a readable zip archive" in failures[0]


def test_the_zip_content_check_rejects_an_empty_archive(config_path, output_root):
    """A valid but empty archive yields no corpus content."""
    path = output_root / "a.zip"
    with zipfile.ZipFile(path, "w"):
        pass
    artifact = Artifact(path=Path("a.zip"), size=path.stat().st_size)
    write_manifest(output_root, [_entry("https://x.test/a.zip", [artifact])])

    failures = validate_downloads(_config(content_checks={".zip": "zip"}), config_path)

    assert len(failures) == 1
    assert "contains no members" in failures[0]


def test_extensions_with_no_declared_check_are_not_checked(config_path, output_root):
    """The content checks are opt-in per extension."""
    artifact = _place(output_root, "a.txt", b"not a pdf")
    write_manifest(output_root, [_entry("https://x.test/a.txt", [artifact])])

    assert validate_downloads(_config(content_checks={".pdf": "pdf"}), config_path) == []


def test_validate_content_checks_accepts_an_absent_table():
    """A config declaring no content checks declares none."""
    assert validate_content_checks({}) == {}


def test_validate_content_checks_lowercases_extensions():
    """An uppercase extension in config still matches a lowercase file."""
    assert validate_content_checks({"content_checks": {".PDF": "pdf"}}) == {".pdf": "pdf"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"content_checks": []}, r"'\[content_checks\]' must be a table"),
        ({"content_checks": {".pdf": "docx"}}, "must be one of"),
        ({"content_checks": {".pdf": 1}}, "must be one of"),
        ({"content_checks": {"pdf": "pdf"}}, "must be a file extension"),
    ],
)
def test_validate_content_checks_rejects_a_malformed_table(raw, expected):
    """A malformed check table fails at config load, not per file."""
    with pytest.raises(ValueError, match=expected):
        validate_content_checks(raw)


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def test_main_exits_zero_on_a_clean_corpus(mocker, config_path, output_root):
    """A corpus matching its manifest exits 0."""
    artifact = _place(output_root, "a.pdf", b"%PDF body")
    write_manifest(output_root, [_entry("https://x.test/a.pdf", [artifact])])
    mocker.patch("sys.argv", ["data-val-downloads", "--config", str(config_path)])

    main()


def test_main_exits_non_zero_on_a_failed_check(mocker, config_path, output_root):
    """A missing artifact exits 1."""
    write_manifest(
        output_root,
        [_entry("https://x.test/a.pdf", [Artifact(path=Path("a.pdf"), size=9)])],
    )
    mocker.patch("sys.argv", ["data-val-downloads", "--config", str(config_path)])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 1


def test_main_exits_non_zero_on_a_missing_manifest(mocker, config_path, output_root):
    """A vacuous pass is the one outcome this validator must never produce."""
    mocker.patch("sys.argv", ["data-val-downloads", "--config", str(config_path)])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 1


def test_main_exits_non_zero_when_the_output_dir_cannot_be_anchored(mocker, tmp_path):
    """An unanchored config exits 1 rather than resolving against the CWD."""
    orphan = tmp_path / "orphan.toml"
    orphan.write_text('output_dir = "data/input/source"\n', encoding="utf-8")
    mocker.patch("sys.argv", ["data-val-downloads", "--config", str(orphan)])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 1
