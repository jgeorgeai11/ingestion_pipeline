"""Tests for ingpipe_acquisition.runner, ingpipe_acquisition.extract, and
ingpipe_acquisition.discover.

Four of these are regression guards for defects the audit found in the two
former instance downloaders, and each is named as such in its docstring: the
exit-0-on-failure defect, the extraction wedge (a skip decided from a
directory created before extraction), the resume hole (a manifest that records
only what this run fetched), and the silent zero-discovery.

Fetching is mocked at ``ingpipe_acquisition.runner.fetch`` -- the network boundary --
so these tests exercise the loop's accounting, skip logic, manifest writing,
and failure isolation rather than re-testing :mod:`ingpipe_acquisition.fetch`.
"""

import zipfile
from pathlib import Path

import pytest
from ingpipe_acquisition.data_validation.data_val_downloads import validate_content_checks
from ingpipe_acquisition.discover import build_discoverer, explicit_targets, templated_targets
from ingpipe_acquisition.extract import ExtractionError, build_post_processor, make_extractor
from ingpipe_acquisition.fetch import FetchError
from ingpipe_acquisition.manifest import (
    STATUS_FAILED,
    STATUS_FETCHED,
    STATUS_SKIPPED,
    Artifact,
    ManifestEntry,
    Target,
    manifest_path,
    read_manifest,
    write_manifest,
)
from ingpipe_acquisition.runner import (
    main,
    resolve_destination,
    run_acquisition,
    validate_config,
)
from ingpipe_lib.testing import assert_example_config_valid


@pytest.fixture
def instance(tmp_path):
    """Build a minimal instance root (a directory with a pyproject.toml).

    ``resolve_config_path`` anchors a relative ``output_dir`` to the nearest
    ancestor of the config that carries a ``pyproject.toml``, so the tests
    need a real one rather than a monkeypatch.
    """
    root = tmp_path / "instance"
    (root / "config").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='t'\n", encoding="utf-8")
    return root


@pytest.fixture
def config_path(instance):
    """Return the path a test config would live at inside the instance."""
    path = instance / "config" / "acquire_test.toml"
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture
def output_root(instance):
    """Return the output root the test configs point at."""
    return instance / "data" / "input" / "source"


def _config(**overrides) -> dict:
    """Build a runner config with fast, network-free defaults."""
    config = {
        "output_dir": "data/input/source",
        "min_targets": 1,
        "request_delay_seconds": 0.0,
        "http": {"retries": 0, "backoff_factor": 0.0, "timeout": 1.0},
    }
    config.update(overrides)
    return config


def _targets(*specs: tuple[str, str]):
    """Build a discoverer yielding the given (url, destination) pairs."""

    def discover(config: dict):
        for url, destination in specs:
            yield Target(url=url, destination=Path(destination))

    return discover


def _fake_fetch(contents: dict[str, bytes], *, failing: set[str] | None = None):
    """Build a stand-in for ``fetch`` that writes canned bytes per URL."""

    def fake(url: str, dest: Path, *, session, max_bytes=None) -> int:
        if failing and url in failing:
            raise FetchError(f"simulated transport failure for {url}")
        payload = contents.get(url, b"payload")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        return len(payload)

    return fake


# ---------------------------------------------------------------------------
# validate_config
# ---------------------------------------------------------------------------


def test_validate_config_accepts_a_minimal_config():
    """The only genuinely required key is the output directory."""
    validate_config({"output_dir": "data/input/source"})


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"output_dir": None}, "'output_dir' is required"),
        ({"output_dir": ""}, "'output_dir' is required"),
        ({"output_dir": 3}, "'output_dir' is required"),
        ({"min_targets": 0}, "'min_targets' must be an integer >= 1"),
        ({"min_targets": True}, "'min_targets' must be an integer >= 1"),
        ({"min_targets": "9"}, "'min_targets' must be an integer >= 1"),
        ({"request_delay_seconds": -1}, "'request_delay_seconds' must be a number >= 0"),
        ({"request_delay_seconds": "2"}, "'request_delay_seconds' must be a number >= 0"),
        ({"dry_run": "yes"}, "'dry_run' must be a boolean"),
        ({"overwrite": 1}, "'overwrite' must be a boolean"),
        ({"max_bytes": 0}, "'max_bytes' must be a positive integer"),
        ({"max_bytes": "big"}, "'max_bytes' must be a positive integer"),
        ({"http": []}, r"'\[http\]' must be a table"),
        ({"http": {"retries": -1}}, "'http.retries' must be an integer >= 0"),
        ({"http": {"backoff_factor": -0.5}}, "'http.backoff_factor' must be a number >= 0"),
        ({"http": {"timeout": 0}}, "'http.timeout' must be a positive number"),
    ],
)
def test_validate_config_rejects_bad_values(overrides, expected):
    """Every runner key is type-checked at load time, not once per target."""
    config = {"output_dir": "data/input/source"}
    config.update(overrides)

    with pytest.raises(ValueError, match=expected):
        validate_config(config)


def test_validate_config_accepts_a_zero_backoff_factor():
    """A zero backoff is legitimate (retry immediately); a zero timeout is not."""
    validate_config({"output_dir": "d", "http": {"backoff_factor": 0}})


def test_the_shipped_example_config_satisfies_both_of_this_package_s_validators() -> None:
    """The annotated config/example.toml this package ships still validates.

    Nothing ever executes the example, so drift from the validators is silent
    and permanent: a required key added above leaves a documented example that
    no longer works, and the cost lands on whoever copies it. This is the only
    thing that runs it.

    Both validators, because the example documents two entry points' configs.
    `[content_checks]` belongs to data-val-downloads and runner.validate_config
    never looks at it, so a validate_config-only check would leave that half of
    the example unguarded -- which is why the block is a real table here rather
    than the commented one it used to be.
    """
    config = assert_example_config_valid("ingpipe_acquisition", validate_config)

    checks = validate_content_checks(config)
    assert checks, (
        "the example's [content_checks] must stay a REAL table; commented out, "
        "it documents a contract nothing checks"
    )


# ---------------------------------------------------------------------------
# The run loop
# ---------------------------------------------------------------------------


def test_run_with_two_succeeding_targets_writes_both_and_reports_no_failures(
    mocker, config_path, output_root
):
    """The happy path: both files land, both are recorded, no failures."""
    mocker.patch("ingpipe_acquisition.runner.fetch", _fake_fetch({}))

    failures = run_acquisition(
        _config(),
        config_path,
        discover=_targets(("https://x.test/a.pdf", "a.pdf"), ("https://x.test/b.pdf", "b.pdf")),
    )

    assert failures == []
    assert (output_root / "a.pdf").read_bytes() == b"payload"
    assert (output_root / "b.pdf").read_bytes() == b"payload"
    entries = read_manifest(output_root)
    assert [entry.status for entry in entries] == [STATUS_FETCHED, STATUS_FETCHED]
    assert entries[0].artifacts == [Artifact(path=Path("a.pdf"), size=7)]


def test_run_where_one_fetch_raises_records_the_other_and_reports_the_failure(
    mocker, config_path, output_root
):
    """Regression guard for the exit-0 defect.

    Both former downloaders counted errors, logged the count, and exited 0, so
    a run that fetched nothing was indistinguishable from a complete one. A
    failed target must come back as a failure message the entry point turns
    into a non-zero exit.
    """
    mocker.patch(
        "ingpipe_acquisition.runner.fetch", _fake_fetch({}, failing={"https://x.test/b.pdf"})
    )

    failures = run_acquisition(
        _config(),
        config_path,
        discover=_targets(("https://x.test/a.pdf", "a.pdf"), ("https://x.test/b.pdf", "b.pdf")),
    )

    assert len(failures) == 1
    assert "b.pdf" in failures[0]
    assert (output_root / "a.pdf").is_file()
    assert not (output_root / "b.pdf").exists()


def test_a_failed_target_is_recorded_as_failed_not_dropped(mocker, config_path, output_root):
    """A partial run's manifest exposes the hole rather than papering over it."""
    mocker.patch(
        "ingpipe_acquisition.runner.fetch", _fake_fetch({}, failing={"https://x.test/b.pdf"})
    )

    run_acquisition(
        _config(),
        config_path,
        discover=_targets(("https://x.test/a.pdf", "a.pdf"), ("https://x.test/b.pdf", "b.pdf")),
    )

    entries = read_manifest(output_root)
    assert [entry.status for entry in entries] == [STATUS_FETCHED, STATUS_FAILED]
    assert entries[1].artifacts == []
    assert "simulated transport failure" in entries[1].error


def test_post_process_failure_fails_only_its_target_and_deletes_its_artifacts(
    mocker, config_path, output_root
):
    """Regression guard for the extraction wedge.

    The old USC script created the extract directory before extracting and
    then treated its existence as "already done", so an extraction that failed
    wedged that title forever. Here a failing post-processor deletes its
    target's artifacts, leaves the other targets alone, and -- because the skip
    decision reads the manifest -- is retried by the next run.
    """
    mocker.patch("ingpipe_acquisition.runner.fetch", _fake_fetch({}))

    def post_process(target: Target, path: Path):
        if target.url.endswith("b.zip"):
            raise ExtractionError("archive contains no matching member")
        return [path]

    config = _config()
    failures = run_acquisition(
        config,
        config_path,
        discover=_targets(("https://x.test/a.zip", "a.zip"), ("https://x.test/b.zip", "b.zip")),
        post_process=post_process,
    )

    assert len(failures) == 1
    assert (output_root / "a.zip").is_file()
    assert not (output_root / "b.zip").exists()

    # The next run retries the failed target rather than skipping it.
    calls: list[str] = []

    def recording_fetch(url, dest, *, session, max_bytes=None):
        calls.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"payload")
        return 7

    mocker.patch("ingpipe_acquisition.runner.fetch", recording_fetch)
    run_acquisition(
        config,
        config_path,
        discover=_targets(("https://x.test/a.zip", "a.zip"), ("https://x.test/b.zip", "b.zip")),
        post_process=lambda target, path: [path],
    )

    assert calls == ["https://x.test/b.zip"]


def test_post_process_returning_no_artifacts_fails_its_target(mocker, config_path, output_root):
    """A target that resolves to nothing is a failure, not an empty success."""
    mocker.patch("ingpipe_acquisition.runner.fetch", _fake_fetch({}))

    failures = run_acquisition(
        _config(),
        config_path,
        discover=_targets(("https://x.test/a.zip", "a.zip")),
        post_process=lambda target, path: [],
    )

    assert len(failures) == 1
    assert "produced no artifacts" in failures[0]


def test_expected_size_mismatch_fails_its_target(mocker, config_path, output_root):
    """A caller-known size is asserted even when the server declares none."""
    mocker.patch("ingpipe_acquisition.runner.fetch", _fake_fetch({}))

    def discover(config):
        yield Target(url="https://x.test/a.pdf", destination=Path("a.pdf"), expected_size=999)

    failures = run_acquisition(_config(), config_path, discover=discover)

    assert len(failures) == 1
    assert "expected 999" in failures[0]


def test_manifest_records_every_discovered_target_across_a_resume(
    mocker, config_path, output_root
):
    """Regression guard for the resume hole.

    A run that fetches some targets, skips others already present from the
    prior manifest, and fails one must still write a manifest listing ALL
    discovered targets. A manifest holding only this run's fetches would make
    validation blind to exactly the files a resume did not reach.
    """
    discover = _targets(
        ("https://x.test/a.pdf", "a.pdf"),
        ("https://x.test/b.pdf", "b.pdf"),
        ("https://x.test/c.pdf", "c.pdf"),
    )

    # First run: everything succeeds.
    mocker.patch("ingpipe_acquisition.runner.fetch", _fake_fetch({}))
    assert run_acquisition(_config(), config_path, discover=discover) == []

    # Second run: 'a' is still on disk and is skipped, 'b' was deleted and is
    # re-fetched, 'c' fails.
    (output_root / "b.pdf").unlink()
    mocker.patch(
        "ingpipe_acquisition.runner.fetch", _fake_fetch({}, failing={"https://x.test/c.pdf"})
    )
    (output_root / "c.pdf").unlink()

    failures = run_acquisition(_config(), config_path, discover=discover)

    assert len(failures) == 1
    entries = read_manifest(output_root)
    assert len(entries) == 3
    assert [entry.status for entry in entries] == [
        STATUS_SKIPPED,
        STATUS_FETCHED,
        STATUS_FAILED,
    ]


def test_a_skipped_target_keeps_its_recorded_artifacts(mocker, config_path, output_root):
    """A skip re-records the prior artifacts, so validation still checks them."""
    discover = _targets(("https://x.test/a.pdf", "a.pdf"))
    mocker.patch("ingpipe_acquisition.runner.fetch", _fake_fetch({}))
    run_acquisition(_config(), config_path, discover=discover)

    def exploding_fetch(*args, **kwargs):
        raise AssertionError("an already-present target must not be re-fetched")

    mocker.patch("ingpipe_acquisition.runner.fetch", exploding_fetch)
    run_acquisition(_config(), config_path, discover=discover)

    entry = read_manifest(output_root)[0]
    assert entry.status == STATUS_SKIPPED
    assert entry.artifacts == [Artifact(path=Path("a.pdf"), size=7)]


def test_a_resized_artifact_is_re_fetched(mocker, config_path, output_root):
    """A file whose size no longer matches the record is not trusted."""
    discover = _targets(("https://x.test/a.pdf", "a.pdf"))
    mocker.patch("ingpipe_acquisition.runner.fetch", _fake_fetch({}))
    run_acquisition(_config(), config_path, discover=discover)

    (output_root / "a.pdf").write_bytes(b"truncated")
    calls: list[str] = []

    def recording_fetch(url, dest, *, session, max_bytes=None):
        calls.append(url)
        dest.write_bytes(b"payload")
        return 7

    mocker.patch("ingpipe_acquisition.runner.fetch", recording_fetch)
    run_acquisition(_config(), config_path, discover=discover)

    assert calls == ["https://x.test/a.pdf"]


def test_overwrite_ignores_the_prior_manifest(mocker, config_path, output_root):
    """``overwrite`` re-fetches everything regardless of what is recorded."""
    discover = _targets(("https://x.test/a.pdf", "a.pdf"))
    mocker.patch("ingpipe_acquisition.runner.fetch", _fake_fetch({}))
    run_acquisition(_config(), config_path, discover=discover)

    calls: list[str] = []

    def recording_fetch(url, dest, *, session, max_bytes=None):
        calls.append(url)
        dest.write_bytes(b"payload")
        return 7

    mocker.patch("ingpipe_acquisition.runner.fetch", recording_fetch)
    run_acquisition(_config(overwrite=True), config_path, discover=discover)

    assert calls == ["https://x.test/a.pdf"]


def test_an_unreadable_prior_manifest_is_ignored_with_a_warning(
    mocker, config_path, output_root, caplog
):
    """A corrupt manifest re-downloads deliberately instead of crashing."""
    output_root.mkdir(parents=True)
    manifest_path(output_root).write_text("{not json", encoding="utf-8")
    mocker.patch("ingpipe_acquisition.runner.fetch", _fake_fetch({}))

    with caplog.at_level("WARNING"):
        failures = run_acquisition(
            _config(), config_path, discover=_targets(("https://x.test/a.pdf", "a.pdf"))
        )

    assert failures == []
    assert "unreadable prior manifest" in caplog.text


def test_a_prior_failed_entry_is_not_treated_as_a_skip(mocker, config_path, output_root):
    """A recorded failure never satisfies the skip test, whatever is on disk."""
    output_root.mkdir(parents=True)
    (output_root / "a.pdf").write_bytes(b"payload")
    write_manifest(
        output_root,
        [
            ManifestEntry(
                url="https://x.test/a.pdf",
                destination=Path("a.pdf"),
                status=STATUS_FAILED,
                artifacts=[],
                error="earlier failure",
            )
        ],
    )
    calls: list[str] = []

    def recording_fetch(url, dest, *, session, max_bytes=None):
        calls.append(url)
        dest.write_bytes(b"payload")
        return 7

    mocker.patch("ingpipe_acquisition.runner.fetch", recording_fetch)
    run_acquisition(_config(), config_path, discover=_targets(("https://x.test/a.pdf", "a.pdf")))

    assert calls == ["https://x.test/a.pdf"]


def test_the_polite_delay_is_paid_only_before_a_fetched_target(mocker, config_path):
    """A resume that skips 37 of 40 targets must not sleep 37 times."""
    discover = _targets(
        ("https://x.test/a.pdf", "a.pdf"),
        ("https://x.test/b.pdf", "b.pdf"),
        ("https://x.test/c.pdf", "c.pdf"),
    )
    mocker.patch("ingpipe_acquisition.runner.fetch", _fake_fetch({}))
    sleep = mocker.patch("ingpipe_acquisition.runner.time.sleep")

    run_acquisition(_config(request_delay_seconds=1.5), config_path, discover=discover)

    # Three fetches, but the delay precedes only the second and third.
    assert sleep.call_count == 2

    sleep.reset_mock()
    run_acquisition(_config(request_delay_seconds=1.5), config_path, discover=discover)

    assert sleep.call_count == 0


# ---------------------------------------------------------------------------
# min_targets, dry_run, and the escape check
# ---------------------------------------------------------------------------


def test_discovery_below_min_targets_fails_before_any_fetch(mocker, config_path):
    """Regression guard for the silent zero-discovery.

    A CMS markup change that broke the link pattern previously logged
    ``Found 0 manual page links`` at INFO and exited 0. The floor turns that
    into a failure, and the failure precedes the first request.
    """
    fetch = mocker.patch("ingpipe_acquisition.runner.fetch")

    with pytest.raises(ValueError, match="below the configured min_targets=5"):
        run_acquisition(
            _config(min_targets=5),
            config_path,
            discover=_targets(("https://x.test/a.pdf", "a.pdf")),
        )

    fetch.assert_not_called()


def test_a_discovery_that_yields_nothing_fails_at_the_default_floor(config_path):
    """The default floor of 1 means an empty discovery can never pass."""

    def discover(config):
        return iter(())

    with pytest.raises(ValueError, match="yielded 0 target"):
        run_acquisition(_config(), config_path, discover=discover)


def test_dry_run_writes_no_file_and_no_manifest_but_logs_every_target(
    mocker, config_path, output_root, caplog
):
    """A dry run previews the whole resolved set and touches nothing."""
    fetch = mocker.patch("ingpipe_acquisition.runner.fetch")

    with caplog.at_level("INFO"):
        failures = run_acquisition(
            _config(dry_run=True),
            config_path,
            discover=_targets(
                ("https://x.test/a.pdf", "a.pdf"), ("https://x.test/b.pdf", "b.pdf")
            ),
        )

    assert failures == []
    fetch.assert_not_called()
    assert not manifest_path(output_root).exists()
    assert "https://x.test/a.pdf" in caplog.text
    assert "https://x.test/b.pdf" in caplog.text


def test_dry_run_still_enforces_min_targets(config_path):
    """A dry run surfaces a broken discovery instead of logging zero targets."""
    with pytest.raises(ValueError, match="below the configured min_targets"):
        run_acquisition(
            _config(dry_run=True, min_targets=3),
            config_path,
            discover=_targets(("https://x.test/a.pdf", "a.pdf")),
        )


def test_dry_run_still_rejects_an_escaping_destination(config_path):
    """The escape check runs in a dry run too, so a preview catches it."""
    with pytest.raises(ValueError, match="escapes the output root"):
        run_acquisition(
            _config(dry_run=True),
            config_path,
            discover=_targets(("https://x.test/a.pdf", "../../escape.pdf")),
        )


def test_a_target_escaping_the_output_root_is_rejected(mocker, config_path):
    """A scraped page influences the destination, so escapes are refused."""
    fetch = mocker.patch("ingpipe_acquisition.runner.fetch")

    with pytest.raises(ValueError, match="escapes the output root"):
        run_acquisition(
            _config(),
            config_path,
            discover=_targets(("https://x.test/a.pdf", "sub/../../a.pdf")),
        )

    fetch.assert_not_called()


def test_an_absolute_destination_is_rejected(config_path):
    """An absolute destination would ignore the output root entirely."""
    with pytest.raises(ValueError, match="must be relative"):
        run_acquisition(
            _config(), config_path, discover=_targets(("https://x.test/a.pdf", "/etc/passwd"))
        )


def test_two_targets_sharing_a_destination_are_rejected(config_path):
    """A destination collision would silently drop one of the two documents."""
    with pytest.raises(ValueError, match="same destination"):
        run_acquisition(
            _config(),
            config_path,
            discover=_targets(("https://x.test/a.pdf", "same.pdf"), ("https://y.test/a.pdf", "same.pdf")),
        )


def test_resolve_destination_allows_the_output_root_itself(tmp_path):
    """A single-file destination directly in the root resolves cleanly."""
    assert resolve_destination(tmp_path, Path("a.pdf")) == (tmp_path / "a.pdf").resolve()


def test_an_artifact_outside_the_output_root_fails_its_target(mocker, config_path, tmp_path):
    """A post-processor that writes outside the corpus fails its target."""
    mocker.patch("ingpipe_acquisition.runner.fetch", _fake_fetch({}))
    stray = tmp_path / "stray.pdf"
    stray.write_bytes(b"x")

    failures = run_acquisition(
        _config(),
        config_path,
        discover=_targets(("https://x.test/a.zip", "a.zip")),
        post_process=lambda target, path: [stray],
    )

    assert len(failures) == 1
    assert "lies outside the output root" in failures[0]


def test_a_relative_output_dir_without_an_instance_root_fails(tmp_path):
    """An unanchored relative output_dir cannot be resolved and must fail."""
    from ingpipe_lib.paths import InstanceRootNotFoundError

    orphan = tmp_path / "orphan.toml"
    orphan.write_text("", encoding="utf-8")

    with pytest.raises(InstanceRootNotFoundError):
        run_acquisition(
            _config(), orphan, discover=_targets(("https://x.test/a.pdf", "a.pdf"))
        )


# ---------------------------------------------------------------------------
# The extractor, exercised through the post-process hook
# ---------------------------------------------------------------------------


def _make_zip(path: Path, members: dict[str, bytes]) -> Path:
    """Write a zip archive containing the given member/content pairs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)
    return path


def test_extractor_keeps_only_matching_members_and_deletes_the_archive(tmp_path):
    """Only ``keep`` members are written; the archive is not corpus content."""
    archive = _make_zip(
        tmp_path / "usc05" / "title.zip",
        {"doc.pdf": b"pdf bytes", "readme.txt": b"ignore me", "notes.htm": b"ignore me"},
    )
    extract = make_extractor(["*.pdf"])

    produced = extract(Target(url="https://x.test/t.zip", destination=Path("usc05/title.zip")), archive)

    assert produced == [tmp_path / "usc05" / "doc.pdf"]
    assert not (tmp_path / "usc05" / "readme.txt").exists()
    assert not archive.exists()


def test_extractor_can_keep_the_archive(tmp_path):
    """``delete_archive=False`` leaves the archive in place."""
    archive = _make_zip(tmp_path / "t.zip", {"doc.pdf": b"pdf"})

    make_extractor(["*.pdf"], delete_archive=False)(
        Target(url="https://x.test/t.zip", destination=Path("t.zip")), archive
    )

    assert archive.exists()


def test_extractor_flattens_when_asked(tmp_path):
    """Flattening discards the archive's internal directory layout."""
    archive = _make_zip(tmp_path / "t.zip", {"deep/nested/doc.pdf": b"pdf"})

    produced = make_extractor(["*.pdf"], flatten=True)(
        Target(url="https://x.test/t.zip", destination=Path("t.zip")), archive
    )

    assert produced == [tmp_path / "doc.pdf"]


def test_extractor_preserves_structure_when_not_flattening(tmp_path):
    """Without flattening the member's own path is preserved."""
    archive = _make_zip(tmp_path / "t.zip", {"deep/doc.pdf": b"pdf"})

    produced = make_extractor(["*.pdf"])(
        Target(url="https://x.test/t.zip", destination=Path("t.zip")), archive
    )

    assert produced == [tmp_path / "deep" / "doc.pdf"]


def test_extractor_with_no_keep_patterns_extracts_everything(tmp_path):
    """``keep=None`` means every member is corpus content."""
    archive = _make_zip(tmp_path / "t.zip", {"a.pdf": b"a", "b.txt": b"b"})

    produced = make_extractor()(
        Target(url="https://x.test/t.zip", destination=Path("t.zip")), archive
    )

    assert [path.name for path in produced] == ["a.pdf", "b.txt"]


def test_extractor_raises_when_no_member_matches(tmp_path):
    """An archive that yields nothing usable fails its target."""
    archive = _make_zip(tmp_path / "t.zip", {"readme.txt": b"nothing useful"})

    with pytest.raises(ExtractionError, match="no member matching"):
        make_extractor(["*.pdf"])(
            Target(url="https://x.test/t.zip", destination=Path("t.zip")), archive
        )

    # The archive survives a failure, so the operator can inspect it.
    assert archive.exists()


def test_extractor_raises_on_a_corrupt_archive(tmp_path):
    """A non-zip file is a failed target, not a crash inside the run loop."""
    archive = tmp_path / "t.zip"
    archive.write_bytes(b"this is not a zip file")

    with pytest.raises(ExtractionError, match="Failed to extract"):
        make_extractor(["*.pdf"])(
            Target(url="https://x.test/t.zip", destination=Path("t.zip")), archive
        )


def test_extractor_rejects_a_member_escaping_the_extraction_directory(tmp_path):
    """Zip-slip: an archive is remote content, so member paths are untrusted."""
    archive = tmp_path / "nested" / "t.zip"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escape.pdf", b"pdf")

    with pytest.raises(ExtractionError, match="escapes the extraction directory"):
        make_extractor(["*.pdf"])(
            Target(url="https://x.test/t.zip", destination=Path("nested/t.zip")), archive
        )

    assert not (tmp_path / "escape.pdf").exists()


def test_extractor_rejects_a_rooted_member(tmp_path):
    """A rooted member leaves the extraction directory without using "..".

    The sibling test above writes "../escape.pdf", which the ``".." in parts``
    clause catches; this covers the OTHER clause, which had no test at all.
    It asserts the OUTCOME rather than which clause produced it: the member
    is refused and nothing is written outside the extraction directory. That
    outcome held even before the rooted clause was made host-independent,
    because the ``.resolve()`` comparison below it is a second line of
    defence -- what the old ``Path.is_absolute()`` cost here was a guard that
    fired on one host and not the other, not an unblocked write.
    """
    archive = tmp_path / "nested" / "t.zip"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("/escape.pdf", b"pdf")

    with pytest.raises(ExtractionError, match="escapes the extraction directory"):
        make_extractor(["*.pdf"])(
            Target(url="https://x.test/t.zip", destination=Path("nested/t.zip")), archive
        )

    assert not (tmp_path / "escape.pdf").exists()
    assert not (archive.parent / "escape.pdf").exists()


def test_extractor_rejects_a_backslash_separated_traversal_member(tmp_path):
    """A Windows-built archive can carry backslash-separated member names.

    Both escape clauses are separator-sensitive, so the member name is
    normalized before either runs: PurePosixPath('..\\\\..\\\\escape.pdf').parts
    is a SINGLE component containing no "..", so without that normalization
    this member passes both clauses on a POSIX host and extracts to a file
    literally named "..\\\\..\\\\escape.pdf".

    Note what this proves on each platform: Python's zipfile normalizes
    separators when READING a member name only when ``os.sep != "/"``, so on
    Windows the extractor already sees "../../escape.pdf" and this is a second
    test of the "/" form. On macOS the backslashes survive and this is the
    regression guard for the normalization itself.
    """
    archive = tmp_path / "nested" / "t.zip"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("..\\..\\escape.pdf", b"pdf")

    with pytest.raises(ExtractionError, match="escapes the extraction directory"):
        make_extractor(["*.pdf"])(
            Target(url="https://x.test/t.zip", destination=Path("nested/t.zip")), archive
        )

    assert not (tmp_path / "escape.pdf").exists()
    assert not (archive.parent / "..\\..\\escape.pdf").exists()


def test_extractor_flattens_a_backslash_separated_member(tmp_path):
    """Flatten must see the member's directory components, whichever separator.

    The flatten split is on "/" only, which is why the normalization happens
    on ENTRY rather than after it: normalized late, "a\\\\b\\\\escape.pdf" would
    reach flatten as one long filename and be kept whole on a POSIX host
    instead of yielding "escape.pdf".
    """
    archive = tmp_path / "t.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a\\b\\escape.pdf", b"pdf")

    produced = make_extractor(["*.pdf"], flatten=True)(
        Target(url="https://x.test/t.zip", destination=Path("t.zip")), archive
    )

    assert produced == [tmp_path / "escape.pdf"]


def test_extractor_removes_a_partial_extraction_on_failure(tmp_path, mocker):
    """A failure mid-extraction leaves no half-written members behind."""
    archive = _make_zip(tmp_path / "t.zip", {"a.pdf": b"a", "b.pdf": b"b"})
    real_open = zipfile.ZipFile.open
    calls = {"n": 0}

    def flaky_open(self, member, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk failure")
        return real_open(self, member, *args, **kwargs)

    mocker.patch.object(zipfile.ZipFile, "open", flaky_open)

    with pytest.raises(ExtractionError, match="Failed to extract"):
        make_extractor(["*.pdf"])(
            Target(url="https://x.test/t.zip", destination=Path("t.zip")), archive
        )

    assert not (tmp_path / "a.pdf").exists()
    assert not (tmp_path / "b.pdf").exists()


def test_the_extractor_runs_through_the_runner_hook(mocker, config_path, output_root):
    """The package's own extraction uses the documented instance interface.

    Extraction is implemented through the ``post_process`` hook rather than
    beside it, so the hook has a real user from day one instead of being a
    dormant branch. This test pins that it works end to end.
    """
    payload = _make_zip(output_root.parent / "fixture.zip", {"doc.pdf": b"pdf bytes"}).read_bytes()
    mocker.patch("ingpipe_acquisition.runner.fetch", _fake_fetch({"https://x.test/t.zip": payload}))

    failures = run_acquisition(
        _config(),
        config_path,
        discover=_targets(("https://x.test/t.zip", "usc05/title.zip")),
        post_process=make_extractor(["*.pdf"], flatten=True),
    )

    assert failures == []
    assert (output_root / "usc05" / "doc.pdf").read_bytes() == b"pdf bytes"
    assert not (output_root / "usc05" / "title.zip").exists()
    entry = read_manifest(output_root)[0]
    assert entry.artifacts == [Artifact(path=Path("usc05/doc.pdf"), size=9)]


def test_an_instance_style_callable_post_processor_is_exercised_through_the_hook(
    mocker, config_path, output_root
):
    """The documented contract: (target, path) -> the artifact paths produced."""
    mocker.patch("ingpipe_acquisition.runner.fetch", _fake_fetch({}))
    seen: list[tuple[str, str]] = []

    def instance_post_process(target: Target, path: Path) -> list[Path]:
        seen.append((target.url, path.name))
        renamed = path.with_name("renamed.pdf")
        path.replace(renamed)
        return [renamed]

    failures = run_acquisition(
        _config(),
        config_path,
        discover=_targets(("https://x.test/a.pdf", "a.pdf")),
        post_process=instance_post_process,
    )

    assert failures == []
    assert seen == [("https://x.test/a.pdf", "a.pdf")]
    assert read_manifest(output_root)[0].artifacts == [
        Artifact(path=Path("renamed.pdf"), size=7)
    ]


# ---------------------------------------------------------------------------
# build_post_processor
# ---------------------------------------------------------------------------


def test_build_post_processor_returns_none_without_an_extract_table():
    """No ``[extract]`` table means each target resolves to its destination."""
    assert build_post_processor({}) is None


def test_build_post_processor_returns_none_when_disabled():
    """``enabled = false`` turns extraction off without deleting the table."""
    assert build_post_processor({"extract": {"enabled": False, "keep": ["*.pdf"]}}) is None


def test_build_post_processor_builds_a_configured_extractor(tmp_path):
    """The built extractor honors keep, flatten, and delete_archive."""
    archive = _make_zip(tmp_path / "t.zip", {"deep/a.pdf": b"a", "b.txt": b"b"})
    extract = build_post_processor(
        {"extract": {"keep": ["*.pdf"], "flatten": True, "delete_archive": False}}
    )

    produced = extract(Target(url="https://x.test/t.zip", destination=Path("t.zip")), archive)

    assert produced == [tmp_path / "a.pdf"]
    assert archive.exists()


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        ([], r"'\[extract\]' must be a table"),
        ({"enabled": "yes"}, "'extract.enabled' must be a boolean"),
        ({"keep": "*.pdf"}, "'extract.keep' must be a list of strings"),
        ({"keep": [1]}, "'extract.keep' must be a list of strings"),
        ({"flatten": "yes"}, "'extract.flatten' must be a boolean"),
        ({"delete_archive": 1}, "'extract.delete_archive' must be a boolean"),
    ],
)
def test_build_post_processor_rejects_a_malformed_extract_table(section, expected):
    """A malformed extract table fails at config load, not per target."""
    with pytest.raises(ValueError, match=expected):
        build_post_processor({"extract": section})


# ---------------------------------------------------------------------------
# The config-driven discoverers
# ---------------------------------------------------------------------------


def test_explicit_targets_yields_the_authored_list():
    """The explicit discoverer is a straight read of the config."""
    config = {
        "discovery": {
            "kind": "explicit",
            "targets": [
                {"url": "https://x.test/a.pdf", "destination": "a.pdf"},
                {"url": "https://x.test/b.pdf", "destination": "sub/b.pdf", "group": "sub"},
            ],
        }
    }

    assert list(explicit_targets(config)) == [
        Target(url="https://x.test/a.pdf", destination=Path("a.pdf")),
        Target(url="https://x.test/b.pdf", destination=Path("sub/b.pdf"), group="sub"),
    ]


@pytest.mark.parametrize(
    ("discovery", "expected"),
    [
        ({"kind": "explicit"}, "'discovery.targets' must be a non-empty list"),
        ({"kind": "explicit", "targets": []}, "'discovery.targets' must be a non-empty list"),
        ({"kind": "explicit", "targets": ["x"]}, r"targets\[0\] must be a table"),
        ({"kind": "explicit", "targets": [{"destination": "a"}]}, "missing a non-empty 'url'"),
        ({"kind": "explicit", "targets": [{"url": "u"}]}, "missing 'destination'"),
        (
            {"kind": "explicit", "targets": [{"url": "u", "destination": "a", "group": 1}]},
            "non-string 'group'",
        ),
        (
            {"kind": "explicit", "targets": [{"url": "u", "destination": ""}]},
            "empty destination",
        ),
        (
            {"kind": "explicit", "targets": [{"url": "u", "destination": "/abs/a.pdf"}]},
            "must be relative",
        ),
        # Drive-relative: absolute under NEITHER platform's rules, so the
        # is_absolute()-based guard admitted it on both hosts -- yet
        # PureWindowsPath('C:/out') / 'D:data/x' is 'D:data/x', off the root.
        (
            {"kind": "explicit", "targets": [{"url": "u", "destination": "D:data/x"}]},
            "must be relative",
        ),
        (
            {"kind": "explicit", "targets": [{"url": "u", "destination": "../a.pdf"}]},
            "escapes the output root",
        ),
    ],
)
def test_explicit_targets_rejects_bad_entries(discovery, expected):
    """An absolute or escaping destination fails naming its target."""
    with pytest.raises(ValueError, match=expected):
        list(explicit_targets({"discovery": discovery}))


def test_templated_targets_substitutes_the_variable_into_every_template():
    """The USC shape: nine titles from one template and one list."""
    config = {
        "discovery": {
            "kind": "templated",
            "variable": "title",
            "values": ["05", "42"],
            "url_template": "https://uscode.test/pdf_usc{title}@119-73not60.zip",
            "destination_template": "usc{title}/pdf_usc{title}.zip",
            "group_template": "usc{title}",
        }
    }

    assert list(templated_targets(config)) == [
        Target(
            url="https://uscode.test/pdf_usc05@119-73not60.zip",
            destination=Path("usc05/pdf_usc05.zip"),
            group="usc05",
        ),
        Target(
            url="https://uscode.test/pdf_usc42@119-73not60.zip",
            destination=Path("usc42/pdf_usc42.zip"),
            group="usc42",
        ),
    ]


def test_templated_targets_omits_the_group_when_no_template_is_given():
    """``group_template`` is optional; its absence yields no group."""
    config = {
        "discovery": {
            "variable": "n",
            "values": [1],
            "url_template": "https://x.test/{n}.pdf",
            "destination_template": "{n}.pdf",
        }
    }

    assert next(iter(templated_targets(config))).group is None


@pytest.mark.parametrize(
    ("discovery", "expected"),
    [
        ({}, "'discovery.variable' must be a non-empty string"),
        ({"variable": ""}, "'discovery.variable' must be a non-empty string"),
        ({"variable": "t"}, "'discovery.values' must be a non-empty list"),
        ({"variable": "t", "values": []}, "'discovery.values' must be a non-empty list"),
        (
            {"variable": "t", "values": ["a"]},
            "'discovery.url_template' must be a non-empty string",
        ),
        (
            {"variable": "t", "values": ["a"], "url_template": "u{t}"},
            "'discovery.destination_template' must be a non-empty string",
        ),
        (
            {
                "variable": "t",
                "values": ["a"],
                "url_template": "u{t}",
                "destination_template": "d{t}",
                "group_template": 7,
            },
            "'discovery.group_template' must be a string",
        ),
        (
            {
                "variable": "t",
                "values": ["a"],
                "url_template": "https://x.test/{year}.pdf",
                "destination_template": "d{t}",
            },
            "references an unknown placeholder",
        ),
        (
            {
                "variable": "t",
                "values": ["a"],
                "url_template": "https://x.test/{t}.pdf",
                "destination_template": "/abs/{t}.pdf",
            },
            "must be relative",
        ),
        # Drive-relative: see the note on the same case in the explicit
        # parametrization above.
        (
            {
                "variable": "t",
                "values": ["a"],
                "url_template": "https://x.test/{t}.pdf",
                "destination_template": "D:data/{t}.pdf",
            },
            "must be relative",
        ),
        (
            {
                "variable": "t",
                "values": ["a"],
                "url_template": "https://x.test/{t}.pdf",
                "destination_template": "../{t}.pdf",
            },
            "escapes the output root",
        ),
    ],
)
def test_templated_targets_rejects_bad_configs(discovery, expected):
    """Template typos are config errors at load, not wrong URLs at run time."""
    with pytest.raises(ValueError, match=expected):
        list(templated_targets({"discovery": discovery}))


def test_build_discoverer_selects_by_kind():
    """``discovery.kind`` names one of the two config-driven discoverers."""
    assert build_discoverer({"discovery": {"kind": "explicit"}}) is explicit_targets
    assert build_discoverer({"discovery": {"kind": "templated"}}) is templated_targets


def test_build_discoverer_requires_a_discovery_table():
    """A config with no discoverer and no code supplies nothing to run."""
    with pytest.raises(ValueError, match=r"'\[discovery\]' is required"):
        build_discoverer({})


def test_build_discoverer_rejects_an_unknown_kind():
    """An unknown kind names the alternative rather than failing opaquely."""
    with pytest.raises(ValueError, match="must be one of"):
        build_discoverer({"discovery": {"kind": "scrape"}})


def test_the_discoverers_are_generators():
    """Discovery is lazy, so a scraping discoverer can interleave its work."""
    config = {
        "discovery": {
            "variable": "t",
            "values": ["a", "b"],
            "url_template": "https://x.test/{t}.pdf",
            "destination_template": "{t}.pdf",
        }
    }
    targets = templated_targets(config)

    assert next(targets).url == "https://x.test/a.pdf"
    assert next(targets).url == "https://x.test/b.pdf"


# ---------------------------------------------------------------------------
# The generic entry point
# ---------------------------------------------------------------------------


def _write_config(path: Path, body: str) -> Path:
    """Write a TOML config at `path` and return it."""
    path.write_text(body, encoding="utf-8")
    return path


def test_main_runs_a_fully_config_driven_source(mocker, instance, config_path, output_root):
    """A source whose targets are computable needs no instance code at all."""
    _write_config(
        config_path,
        """
output_dir = "data/input/source"
min_targets = 2
request_delay_seconds = 0.0

[discovery]
kind = "templated"
variable = "title"
values = ["05", "42"]
url_template = "https://uscode.test/pdf_usc{title}.zip"
destination_template = "usc{title}/pdf_usc{title}.zip"
""",
    )
    mocker.patch("ingpipe_acquisition.runner.fetch", _fake_fetch({}))
    mocker.patch("sys.argv", ["acquire", "--config", str(config_path)])

    main()

    assert (output_root / "usc05" / "pdf_usc05.zip").is_file()
    assert len(read_manifest(output_root)) == 2


def test_main_exits_non_zero_when_a_target_fails(mocker, config_path):
    """Regression guard for the exit-0 defect, at the entry-point boundary."""
    _write_config(
        config_path,
        """
output_dir = "data/input/source"
request_delay_seconds = 0.0

[discovery]
kind = "explicit"
targets = [{ url = "https://x.test/a.pdf", destination = "a.pdf" }]
""",
    )
    mocker.patch(
        "ingpipe_acquisition.runner.fetch", _fake_fetch({}, failing={"https://x.test/a.pdf"})
    )
    mocker.patch("sys.argv", ["acquire", "--config", str(config_path)])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 1


def test_main_exits_non_zero_on_an_invalid_config(mocker, config_path):
    """A config error aborts before any network activity."""
    _write_config(config_path, 'min_targets = 0\noutput_dir = "data/input/source"\n')
    mocker.patch("sys.argv", ["acquire", "--config", str(config_path)])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 1


def test_main_exits_non_zero_when_the_output_dir_cannot_be_anchored(mocker, tmp_path):
    """A relative output_dir with no instance root above it fails loudly."""
    orphan = tmp_path / "orphan.toml"
    _write_config(
        orphan,
        """
output_dir = "data/input/source"

[discovery]
kind = "explicit"
targets = [{ url = "https://x.test/a.pdf", destination = "a.pdf" }]
""",
    )
    mocker.patch("sys.argv", ["acquire", "--config", str(orphan)])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 1


def test_main_accepts_an_instance_supplied_discoverer(mocker, config_path, output_root):
    """A scraping source passes its callable through a three-line wrapper."""
    _write_config(config_path, 'output_dir = "data/input/source"\nrequest_delay_seconds = 0.0\n')
    mocker.patch("ingpipe_acquisition.runner.fetch", _fake_fetch({}))
    mocker.patch("sys.argv", ["download-x", "--config", str(config_path)])

    main(discover=_targets(("https://x.test/a.pdf", "a.pdf")))

    assert (output_root / "a.pdf").is_file()


# ---------------------------------------------------------------------------
# Residual cleanup branches
# ---------------------------------------------------------------------------


def test_a_member_resolving_onto_the_extraction_directory_is_rejected(tmp_path):
    """A degenerate member name that resolves to the directory itself fails."""
    archive = tmp_path / "t.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(".", b"degenerate")

    with pytest.raises(ExtractionError, match="escapes the extraction directory"):
        make_extractor()(Target(url="https://x.test/t.zip", destination=Path("t.zip")), archive)


def test_partial_extraction_cleanup_warns_but_does_not_mask_the_failure(tmp_path, mocker, caplog):
    """A cleanup that itself fails is logged, not swallowed into the traceback."""
    archive = _make_zip(tmp_path / "t.zip", {"a.pdf": b"a", "b.pdf": b"b"})
    real_open = zipfile.ZipFile.open
    calls = {"n": 0}

    def flaky_open(self, member, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk failure")
        return real_open(self, member, *args, **kwargs)

    mocker.patch.object(zipfile.ZipFile, "open", flaky_open)
    mocker.patch.object(Path, "unlink", side_effect=OSError("permission denied"))

    with caplog.at_level("WARNING"):
        with pytest.raises(ExtractionError, match="Failed to extract"):
            make_extractor()(
                Target(url="https://x.test/t.zip", destination=Path("t.zip")), archive
            )

    assert "Could not remove partially extracted" in caplog.text


def test_artifact_cleanup_failure_is_warned_not_raised(mocker, config_path, output_root, caplog):
    """A failed target whose cleanup fails still fails only that target."""
    mocker.patch("ingpipe_acquisition.runner.fetch", _fake_fetch({}))
    mocker.patch.object(Path, "unlink", side_effect=OSError("permission denied"))

    def post_process(target, path):
        raise ExtractionError("unusable arrival")

    with caplog.at_level("WARNING"):
        failures = run_acquisition(
            _config(),
            config_path,
            discover=_targets(("https://x.test/a.zip", "a.zip")),
            post_process=post_process,
        )

    assert len(failures) == 1
    assert "Could not remove" in caplog.text
