"""Unit tests for instance-root discovery and path anchoring (ingpipe_lib.paths)."""

from pathlib import Path

import pytest
from ingpipe_lib.paths import (
    InstanceRootNotFoundError,
    fallback_log_root,
    find_instance_root,
    is_rooted_path,
    require_instance_root,
    resolve_config_path,
    resolve_log_dir,
)


def _make_instance(tmp_path: Path) -> tuple[Path, Path]:
    """Build a minimal instance tree: root marker + a nested config file.

    Args:
        tmp_path: Base temporary directory.

    Returns:
        Tuple of (instance_root, config_path).
    """
    instance = tmp_path / "instances" / "policy_db"
    config_dir = instance / "config" / "ingpipe_file_ingestion" / "cms_iom"
    config_dir.mkdir(parents=True)
    (instance / "pyproject.toml").write_text('[project]\nname = "x"\n')
    config_path = config_dir / "ingest_something.toml"
    config_path.write_text('source_dir = "data/input/cms_iom"\n')
    return instance, config_path


class TestIsRootedPath:
    # Every form that carries a drive or a root under EITHER platform's rules,
    # so joining it under a root would not stay under that root. The three
    # marked below are the regression guard: they are absolute under NEITHER
    # rule set, so the `is_absolute()`-based predicate this replaced admitted
    # them while they still escaped.
    @pytest.mark.parametrize(
        "raw",
        [
            "/data/in",
            "C:/data",
            "C:data",  # drive-relative: absolute under neither rule set
            "D:data",  # drive-relative: absolute under neither rule set
            r"\etc\passwd",  # rooted but drive-less: absolute under neither
            "//server/share",
            r"\\server\share",
        ],
    )
    def test_is_rooted_path_accepts_every_rooted_form(self, raw: str) -> None:
        assert is_rooted_path(raw) is True

    @pytest.mark.parametrize("raw", ["data/in", "../data/in"])
    def test_is_rooted_path_rejects_genuinely_relative_values(self, raw: str) -> None:
        assert is_rooted_path(raw) is False


class TestFindInstanceRoot:
    def test_find_instance_root_walks_up_from_nested_config(self, tmp_path: Path) -> None:
        instance, config_path = _make_instance(tmp_path)
        assert find_instance_root(config_path) == instance

    def test_find_instance_root_accepts_a_directory_start(self, tmp_path: Path) -> None:
        instance, config_path = _make_instance(tmp_path)
        assert find_instance_root(config_path.parent) == instance

    def test_find_instance_root_returns_none_outside_any_instance(self, tmp_path: Path) -> None:
        stray = tmp_path / "nowhere" / "config.toml"
        stray.parent.mkdir(parents=True)
        stray.write_text("")
        assert find_instance_root(stray) is None

    def test_find_instance_root_prefers_the_nearest_marker(self, tmp_path: Path) -> None:
        # A workspace root above the instance must not win over the
        # instance's own marker.
        (tmp_path / "pyproject.toml").write_text("[tool.uv.workspace]\n")
        instance, config_path = _make_instance(tmp_path)
        assert find_instance_root(config_path) == instance


class TestRequireInstanceRoot:
    def test_require_instance_root_error_names_the_config_path(self, tmp_path: Path) -> None:
        stray = tmp_path / "config.toml"
        stray.write_text("")
        with pytest.raises(InstanceRootNotFoundError, match=r"config\.toml"):
            require_instance_root(stray)


class TestResolveConfigPath:
    def test_resolve_config_path_anchors_relative_to_instance_root(self, tmp_path: Path) -> None:
        instance, config_path = _make_instance(tmp_path)
        resolved = resolve_config_path("data/input/cms_iom", config_path)
        assert resolved == instance / "data" / "input" / "cms_iom"

    def test_resolve_config_path_identical_from_three_working_directories(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The whole point of anchoring: an installed script may run from
        # anywhere, and the same config must resolve to the same place.
        instance, config_path = _make_instance(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()

        results = []
        for cwd in (instance, elsewhere, tmp_path):
            monkeypatch.chdir(cwd)
            results.append(resolve_config_path("data/input/cms_iom", config_path))

        assert results[0] == results[1] == results[2] == instance / "data" / "input" / "cms_iom"

    def test_resolve_config_path_returns_absolute_paths_unchanged(self, tmp_path: Path) -> None:
        _, config_path = _make_instance(tmp_path)
        absolute = tmp_path / "somewhere" / "else"
        assert resolve_config_path(absolute, config_path) == absolute

    def test_resolve_config_path_rejects_a_path_rooted_only_off_host(
        self, tmp_path: Path
    ) -> None:
        # A drive-relative value is absolute under NEITHER rule set, so this
        # case is rejected identically on both development machines -- unlike
        # "/data/in" or "C:/data", each of which is genuinely absolute on one
        # of them and would take the pass-through branch there.
        _, config_path = _make_instance(tmp_path)
        with pytest.raises(ValueError, match=r"D:data/x.*relative to the instance root"):
            resolve_config_path("D:data/x", config_path)

    def test_resolve_config_path_outside_instance_fails_clearly(self, tmp_path: Path) -> None:
        stray = tmp_path / "stray" / "config.toml"
        stray.parent.mkdir(parents=True)
        stray.write_text('source_dir = "data/input"\n')
        with pytest.raises(InstanceRootNotFoundError, match="No instance root found"):
            resolve_config_path("data/input", stray)


class TestResolveLogDir:
    def test_resolve_log_dir_anchors_under_the_instance(self, tmp_path: Path) -> None:
        instance, config_path = _make_instance(tmp_path)
        assert (
            resolve_log_dir("ingpipe_file_ingestion", config_path)
            == instance / "logs" / "ingpipe_file_ingestion"
        )

    def test_resolve_log_dir_without_config_falls_back_to_temp_location(self) -> None:
        assert (
            resolve_log_dir("ingpipe_file_ingestion")
            == fallback_log_root() / "ingpipe_file_ingestion"
        )

    def test_resolve_log_dir_outside_instance_falls_back_to_temp_location(
        self, tmp_path: Path
    ) -> None:
        stray = tmp_path / "config.toml"
        stray.write_text("")
        assert (
            resolve_log_dir("ingpipe_excel_ingestion", stray)
            == fallback_log_root() / "ingpipe_excel_ingestion"
        )
