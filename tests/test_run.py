"""Tests for the run.py entry point (venv setup, startup report, launch)."""

import sys
from unittest.mock import MagicMock

import pytest

import run


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("PROVIDER_MODEL", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


class TestSetupVenv:
    def test_skips_existing_venv(self, monkeypatch, tmp_path):
        python = tmp_path / "python3"
        python.touch()
        monkeypatch.setattr(run, "VENV_PYTHON", python)
        create = MagicMock()
        monkeypatch.setattr(run.venv, "create", create)
        run.setup_venv()
        create.assert_not_called()

    def test_creates_venv_and_installs(self, monkeypatch, tmp_path):
        monkeypatch.setattr(run, "VENV_DIR", tmp_path / "venv")
        monkeypatch.setattr(run, "VENV_PYTHON", tmp_path / "venv/python3")
        create = MagicMock()
        check_call = MagicMock()
        monkeypatch.setattr(run.venv, "create", create)
        monkeypatch.setattr(run.subprocess, "check_call", check_call)

        run.setup_venv()

        create.assert_called_once_with(tmp_path / "venv", with_pip=True)
        command = check_call.call_args.args[0]
        assert command[1:5] == ["-m", "pip", "install", "-r"]
        assert command[5] == str(run.REQUIREMENTS)


class TestReportProvider:
    def test_reports_provider_and_model(self, capsys, notebook_output_dir):
        assert run.report_provider() is True
        out = capsys.readouterr().out
        assert "LLM provider: anthropic (claude-opus-5-5)" in out
        assert "API key found in ANTHROPIC_API_KEY" in out
        assert f"Output folder: {notebook_output_dir}" in out
        assert "will fail" not in out

    def test_warns_about_missing_provider(self, capsys, monkeypatch):
        monkeypatch.delenv("PROVIDER")
        assert run.report_provider() is False
        out = capsys.readouterr().out
        assert "export PROVIDER" in out
        assert "will fail until the variables above are exported" in out

    def test_warns_about_missing_output_dir(self, capsys, monkeypatch):
        monkeypatch.delenv("NOTEBOOK_OUTPUT_DIR")
        assert run.report_provider() is False
        assert "export NOTEBOOK_OUTPUT_DIR" in capsys.readouterr().out

    def test_warns_about_missing_key(self, capsys, monkeypatch):
        monkeypatch.setenv("PROVIDER", "gemini")
        run.report_provider()
        assert "Export GEMINI_API_KEY" in capsys.readouterr().out

    def test_warns_about_bad_provider(self, capsys, monkeypatch):
        monkeypatch.setenv("PROVIDER", "openai")
        run.report_provider()
        assert "not configured" in capsys.readouterr().out


class TestMain:
    @pytest.fixture
    def mocks(self, monkeypatch):
        mocks = {
            "setup_venv": MagicMock(),
            "execv": MagicMock(),
            "uvicorn_run": MagicMock(),
        }
        monkeypatch.setattr(run, "setup_venv", mocks["setup_venv"])
        monkeypatch.setattr(run.os, "execv", mocks["execv"])
        monkeypatch.setitem(
            sys.modules, "uvicorn", MagicMock(run=mocks["uvicorn_run"])
        )
        return mocks

    def test_starts_server_inside_venv(self, mocks, monkeypatch):
        monkeypatch.setattr(run.sys, "executable", str(run.VENV_PYTHON))
        run.main()
        mocks["execv"].assert_not_called()
        mocks["uvicorn_run"].assert_called_once_with(
            "api.main:app", host=run.HOST, port=run.PORT, reload=True
        )

    def test_reexecs_with_venv_python(self, mocks, monkeypatch):
        monkeypatch.setattr(run.sys, "executable", "/usr/bin/python3")
        run.main()
        mocks["execv"].assert_called_once_with(
            run.VENV_PYTHON, [str(run.VENV_PYTHON), run.__file__]
        )
