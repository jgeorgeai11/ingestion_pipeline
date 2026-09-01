"""Unit tests for ingpipe_lib.cli (the shared entry-point preamble)."""

import logging
from pathlib import Path

import pytest
from ingpipe_lib.cli import (
    RUN_SEPARATOR,
    build_parser,
    finish_run,
    load_config,
    run_scope,
    setup_entry_logging,
)
from pytest_mock import MockerFixture


class TestBuildParser:
    """Tests for the canonical argument pair."""

    def test_config_is_required(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A flag-less invocation is an argparse usage error (exit 2)."""
        parser = build_parser("desc")
        with pytest.raises(SystemExit) as exc:
            parser.parse_args([])
        assert exc.value.code == 2
        assert "--config" in capsys.readouterr().err

    def test_env_file_required_for_db_entry_points(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """With env_file=True (default), --env-file is required."""
        parser = build_parser("desc")
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["--config", "x.toml"])
        assert exc.value.code == 2
        assert "--env-file" in capsys.readouterr().err

    def test_env_file_absent_for_non_db_entry_points(self) -> None:
        """With env_file=False the parser takes only --config."""
        parser = build_parser("desc", env_file=False)
        args = parser.parse_args(["--config", "x.toml"])
        assert args.config == "x.toml"
        assert not hasattr(args, "env_file")

    def test_caller_can_add_script_specific_arguments(self) -> None:
        """The returned parser accepts additional arguments before parsing."""
        parser = build_parser("desc")
        parser.add_argument("--overwrite", action="store_true", default=None)
        args = parser.parse_args(
            ["--config", "x.toml", "--env-file", ".env", "--overwrite"]
        )
        assert args.overwrite is True


class TestSetupEntryLogging:
    """Tests for the INFO-level, config-stem-named logging setup."""

    def test_log_named_from_config_stem_at_info_level(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """The log file is named from the CONFIG stem and the level is INFO."""
        mock_setup = mocker.patch("ingpipe_lib.cli.setup_logging")
        # Mark tmp_path as an instance root so resolve_log_dir anchors there.
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        config_path = tmp_path / "config" / "ingest_qpp_cm_2026.toml"

        setup_entry_logging("ingpipe_excel_ingestion", config_path)

        kwargs = mock_setup.call_args.kwargs
        assert kwargs["log_name"] == "ingest_qpp_cm_2026"
        assert kwargs["level"] == logging.INFO
        assert kwargs["log_dir"] == tmp_path / "logs" / "ingpipe_excel_ingestion"


class TestRunScope:
    """Tests for the run-boundary separators."""

    def test_separators_bracket_a_normal_run(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Open and close separators surround the body's messages."""
        with caplog.at_level(logging.INFO, logger="ingpipe_lib.cli"):
            with run_scope():
                pass
        separators = [
            r for r in caplog.records if r.message == RUN_SEPARATOR
        ]
        assert len(separators) == 2

    def test_closing_separator_emitted_on_sys_exit(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """sys.exit inside the scope still terminates the run block."""
        with caplog.at_level(logging.INFO, logger="ingpipe_lib.cli"):
            with pytest.raises(SystemExit):
                with run_scope():
                    raise SystemExit(1)
        separators = [
            r for r in caplog.records if r.message == RUN_SEPARATOR
        ]
        assert len(separators) == 2


class TestLoadConfig:
    """Tests for the env-file + config loading sequence."""

    def test_missing_env_file_exits_one(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A missing --env-file path exits 1 with an error naming it."""
        config_path = tmp_path / "config.toml"
        config_path.write_text("a = 1\n", encoding="utf-8")
        missing_env = tmp_path / "no-such.env"

        with caplog.at_level(logging.ERROR):
            with pytest.raises(SystemExit) as exc:
                load_config(config_path, missing_env)

        assert exc.value.code == 1
        assert any("no-such.env" in r.message for r in caplog.records)

    def test_missing_config_exits_one_naming_path(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A missing config file exits 1 with an error naming the path."""
        missing = tmp_path / "absent.toml"

        with caplog.at_level(logging.ERROR):
            with pytest.raises(SystemExit) as exc:
                load_config(missing)

        assert exc.value.code == 1
        assert any("absent.toml" in r.message for r in caplog.records)

    def test_malformed_toml_exits_one_naming_path(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Malformed TOML exits 1 with an error naming the path."""
        config_path = tmp_path / "bad.toml"
        config_path.write_text("this is = not valid = toml ][", encoding="utf-8")

        with caplog.at_level(logging.ERROR):
            with pytest.raises(SystemExit) as exc:
                load_config(config_path)

        assert exc.value.code == 1
        assert any("bad.toml" in r.message for r in caplog.records)

    def test_valid_config_returns_parsed_dict(self, tmp_path: Path) -> None:
        """A well-formed config (with a real env file) parses to a dict."""
        config_path = tmp_path / "good.toml"
        config_path.write_text('name = "x"\n[load]\nrun = false\n', encoding="utf-8")
        env_path = tmp_path / ".env.empty"
        env_path.write_text("", encoding="utf-8")

        config = load_config(config_path, env_path)

        assert config == {"name": "x", "load": {"run": False}}

    def test_none_env_file_skips_env_loading(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        """Non-DB entry points pass env_file=None and load no dotenv."""
        mock_load_env = mocker.patch("ingpipe_lib.cli.load_env")
        config_path = tmp_path / "good.toml"
        config_path.write_text("a = 1\n", encoding="utf-8")

        load_config(config_path, None)

        mock_load_env.assert_not_called()


class TestFinishRun:
    """Tests for the failure-accumulation tail."""

    def test_empty_failures_logs_success_and_returns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No failures: the success message logs at INFO and no exit occurs."""
        with caplog.at_level(logging.INFO, logger="ingpipe_lib.cli"):
            finish_run([], success_message="ALL GOOD", failure_prefix="FAILED")

        assert any(r.message == "ALL GOOD" for r in caplog.records)

    def test_failures_log_each_and_exit_one(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Failures: counted summary + each failure at ERROR, then exit 1."""
        failures = ["FAIL: first", "FAIL: second"]

        with caplog.at_level(logging.ERROR):
            with pytest.raises(SystemExit) as exc:
                finish_run(
                    failures, success_message="ALL GOOD", failure_prefix="FAILED"
                )

        assert exc.value.code == 1
        messages = [r.message for r in caplog.records]
        assert "FAILED: 2 failure(s)" in messages
        assert "FAIL: first" in messages
        assert "FAIL: second" in messages
        assert all(
            r.levelno == logging.ERROR
            for r in caplog.records
            if r.name == "ingpipe_lib.cli"
        )
