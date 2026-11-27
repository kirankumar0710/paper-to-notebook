"""Tests for the optional "check the notebook runs" step.

Runner tests start real Python processes with this interpreter (no
sandbox venv, no package installs). Repair tests use a fake runner and a
fake provider, so no code is run and no API calls are made.
"""

import io
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
import notebook_builder.sandbox_runner as sandbox_runner
import notebook_builder.verify as verify_module
from notebook_builder import pdf_to_notebook
from notebook_builder.verify import (
    PROTOCOL_PREFIX,
    REPAIR_SCHEMA,
    RunResult,
    SandboxError,
    VerifyReport,
    VerifySettings,
    load_verify_settings,
    run_cells,
    sandbox_python,
    verify_allowed,
    verify_notebook,
)
from providers import LLMProvider, ProviderError, ProviderSettings
from providers.base import Progress

PDF = b"%PDF-1.7 fake paper"

SPEC = {
    "title": "Tiny Paper",
    "pip_packages": ["einops"],
    "requires_gpu": False,
    "cells": [
        {"cell_type": "markdown", "source": "## Method"},
        {"cell_type": "code", "source": "steps = 1 if QUICK_RUN else 100"},
        {"cell_type": "markdown", "source": "## Train"},
        {"cell_type": "code", "source": "print(stepz)"},
    ],
}


def _settings(**overrides):
    return VerifySettings(python=sys.executable, **overrides)


class RepairProvider(LLMProvider):
    """Returns the queued replies (or raises them), one per call."""

    def __init__(self, *replies):
        super().__init__(ProviderSettings(name="fake", model="fake-model"))
        self.replies = list(replies)
        self.calls = []

    def generate_json(
        self, prompt, *, schema, system=None, pdf=None, on_progress=None
    ):
        self.calls.append({"prompt": prompt, "schema": schema, "pdf": pdf})
        if on_progress is not None:
            on_progress(Progress("thinking", "Thinking…", thinking="Hmm"))
            on_progress(
                Progress(
                    "done",
                    "Done.",
                    usage={
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                    },
                )
            )
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeRunner:
    """Fails each run at the given cell indexes, then passes."""

    def __init__(self, *failures):
        self.failures = list(failures)
        self.runs = []

    def __call__(self, python, cells, settings, on_event):
        self.runs.append(list(cells))
        failed = self.failures[0] if self.failures else len(cells)
        for index in range(min(failed + 1, len(cells))):
            on_event({"event": "start", "cell": index})
            if index != failed:
                on_event(
                    {
                        "event": "ok",
                        "cell": index,
                        "seconds": 0.1,
                        "output": "",
                    }
                )
        if self.failures:
            index = self.failures.pop(0)
            return RunResult(
                False,
                failed=index,
                error="NameError: name 'stepz' is not defined",
                details="Traceback ...",
            )
        return RunResult(True)


def _prepare(settings, notify):
    return sys.executable


FIX = {
    "explanation": "Typo in the variable name.",
    "pip_packages": ["einops"],
    "fixes": [{"cell": 2, "source": "print(steps)"}],
}


class TestSettings:
    def test_defaults(self):
        settings = load_verify_settings({})
        assert settings == VerifySettings()
        assert settings.default is False
        assert settings.allow_remote is False

    def test_shipped_config(self):
        settings = load_verify_settings()
        assert settings.default is False
        assert settings.max_repairs == 2
        assert "torch" in settings.base_packages
        assert settings.sandbox_dir.is_absolute()

    def test_values_are_read(self, tmp_path):
        settings = load_verify_settings(
            {
                "verify": {
                    "default": True,
                    "cell_timeout": 5,
                    "max_repairs": 0,
                    "python": "~/py",
                    "sandbox_dir": str(tmp_path),
                    "base_packages": [" numpy "],
                }
            }
        )
        assert settings.default is True
        assert settings.cell_timeout == 5.0
        assert settings.max_repairs == 0
        assert settings.python == str(Path("~/py").expanduser())
        assert settings.sandbox_dir == tmp_path
        assert settings.base_packages == ("numpy",)

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("default", "yes"),
            ("cell_timeout", -1),
            ("cell_timeout", True),
            ("max_repairs", 99),
            ("base_packages", ["", 3]),
            ("sandbox_dir", " "),
        ],
    )
    def test_invalid_values_fall_back(self, key, value, caplog):
        settings = load_verify_settings({"verify": {key: value}})
        assert getattr(settings, key) == getattr(VerifySettings(), key)
        assert f"Ignoring invalid [verify] {key}" in caplog.text

    @pytest.mark.parametrize(
        ("host", "allow_remote", "allowed"),
        [
            ("127.0.0.1", False, True),
            ("::1", False, True),
            ("10.0.0.5", False, False),
            (None, False, False),
            ("10.0.0.5", True, True),
        ],
    )
    def test_only_localhost_by_default(self, host, allow_remote, allowed):
        settings = VerifySettings(allow_remote=allow_remote)
        assert verify_allowed(host, settings) is allowed


class TestSandboxRunner:
    def test_prefix_matches(self):
        assert sandbox_runner.PREFIX == PROTOCOL_PREFIX

    def test_shell_and_magic_lines(self):
        source = "%matplotlib inline\nif True:\n    !echo hi\nx = 1"
        assert sandbox_runner.to_python(source) == (
            "pass\nif True:\n    _p2n_shell('echo hi')\nx = 1"
        )

    def test_run_reports_each_cell(self):
        stream = io.StringIO()
        assert sandbox_runner.run(["a = 1", "display(a)"], stream) == 0
        events = [
            json.loads(line[len(PROTOCOL_PREFIX) :])["event"]
            for line in stream.getvalue().splitlines()
        ]
        assert events == ["start", "ok", "start", "ok", "finished"]

    def test_run_stops_at_error(self):
        stream = io.StringIO()
        assert sandbox_runner.run(["print('x')", "1/0", "never"], stream)
        last = json.loads(
            stream.getvalue().splitlines()[-1][len(PROTOCOL_PREFIX) :]
        )
        assert last["event"] == "error"
        assert last["cell"] == 1
        assert last["error"] == "ZeroDivisionError: division by zero"
        assert "<cell 1>" in last["traceback"]

    def test_main_reads_stdin(self, monkeypatch, capsys):
        monkeypatch.setattr(
            sys, "stdin", io.StringIO(json.dumps({"cells": ["x = 1"]}))
        )
        assert sandbox_runner.main() == 0
        assert '"finished"' in capsys.readouterr().out


class TestRunCells:
    def test_all_cells_pass(self):
        events = []
        cells = [
            "QUICK_RUN = True",
            "import os\nprint('noise')\n!echo shell-ok",
            "assert QUICK_RUN\nos.system('echo from-a-subprocess')",
        ]
        result = run_cells(sys.executable, cells, _settings(), events.append)
        assert result == RunResult(True)
        assert [(e["event"], e["cell"]) for e in events] == [
            ("start", 0),
            ("ok", 0),
            ("start", 1),
            ("ok", 1),
            ("start", 2),
            ("ok", 2),
        ]
        assert "noise" in events[3]["output"]
        assert "shell-ok" in events[3]["output"]
        assert events[3]["seconds"] >= 0

    def test_failure_has_traceback_and_output(self):
        result = run_cells(
            sys.executable,
            ["print('before')", "undefined_name"],
            _settings(),
        )
        assert not result.ok
        assert result.failed == 1
        assert (
            result.error == "NameError: name 'undefined_name' is not defined"
        )
        assert "Traceback" in result.details

    def test_no_api_keys_in_environment(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
        result = run_cells(
            sys.executable,
            ["import os\nassert 'ANTHROPIC_API_KEY' not in os.environ"],
            _settings(),
        )
        assert result.ok

    def test_cell_timeout(self):
        started = time.monotonic()
        result = run_cells(
            sys.executable,
            ["x = 1", "import time\ntime.sleep(30)"],
            _settings(cell_timeout=0.5),
        )
        assert time.monotonic() - started < 10
        assert result.timed_out
        assert result.failed == 1
        assert "over 0 s" in result.error or "over 1 s" in result.error
        assert "QUICK_RUN" in result.details

    def test_total_timeout(self):
        result = run_cells(
            sys.executable,
            ["import time\ntime.sleep(0.3)"] * 10,
            _settings(cell_timeout=5, total_timeout=0.5),
        )
        assert result.timed_out
        assert "whole notebook" in result.error

    def test_crash_is_reported(self):
        result = run_cells(
            sys.executable, ["import os\nos._exit(3)"], _settings()
        )
        assert not result.ok
        assert result.failed == 0
        assert "exited with code 3" in result.error

    def test_missing_interpreter(self, tmp_path):
        result = run_cells(str(tmp_path / "nope"), ["x = 1"], _settings())
        assert not result.ok
        assert "Could not start" in result.error


class TestSandboxPython:
    def test_configured_interpreter(self):
        assert sandbox_python(_settings()) == sys.executable

    def test_missing_configured_interpreter(self):
        with pytest.raises(SandboxError, match="was not found"):
            sandbox_python(VerifySettings(python="/no/such/python"))

    def _managed(self, tmp_path, monkeypatch, run=None):
        created, installs, notes = [], [], []

        def fake_create(directory, with_pip):
            created.append(directory)
            python = verify_module._venv_python(directory)
            python.parent.mkdir(parents=True)
            python.touch()

        def fake_run(args, **kwargs):
            installs.append(args)
            if run is not None:
                run(args)

        monkeypatch.setattr(verify_module.venv, "create", fake_create)
        monkeypatch.setattr(verify_module.subprocess, "run", fake_run)
        settings = VerifySettings(
            sandbox_dir=tmp_path / "env", base_packages=("numpy",)
        )
        return settings, created, installs, notes

    def test_created_once(self, tmp_path, monkeypatch):
        settings, created, installs, notes = self._managed(
            tmp_path, monkeypatch
        )
        first = sandbox_python(settings, notes.append)
        second = sandbox_python(settings, notes.append)
        assert first == second
        assert first.startswith(str(tmp_path / "env"))
        assert len(created) == 1
        assert len(installs) == 1
        assert installs[0][-1] == "numpy"
        assert "first run only" in notes[0]

    def test_new_packages_reinstall(self, tmp_path, monkeypatch):
        settings, created, installs, _ = self._managed(tmp_path, monkeypatch)
        sandbox_python(settings)
        sandbox_python(
            VerifySettings(
                sandbox_dir=settings.sandbox_dir, base_packages=("scipy",)
            )
        )
        assert len(created) == 1
        assert [args[-1] for args in installs] == ["numpy", "scipy"]

    @pytest.mark.parametrize(
        ("error", "message"),
        [
            (
                subprocess.CalledProcessError(1, "pip", stderr="no wheel"),
                "no wheel",
            ),
            (subprocess.TimeoutExpired("pip", 1), "took longer"),
            (OSError("disk full"), "disk full"),
        ],
    )
    def test_setup_failures(self, tmp_path, monkeypatch, error, message):
        def fail(args):
            raise error

        settings, *_ = self._managed(tmp_path, monkeypatch, run=fail)
        with pytest.raises(SandboxError, match=message):
            sandbox_python(settings)


class TestVerifyNotebook:
    def _verify(self, provider, runner, settings=None, spec=SPEC, **kw):
        updates = []
        new_spec, report = verify_notebook(
            spec,
            provider,
            settings or _settings(),
            on_progress=updates.append,
            runner=runner,
            prepare=kw.get("prepare", _prepare),
        )
        return new_spec, report, updates

    def test_passes_first_time(self):
        runner = FakeRunner()
        spec, report, updates = self._verify(RepairProvider(), runner)
        assert report.status == "passed"
        assert report.repairs == 0
        assert report.code_cells == 2
        assert report.usage is None
        assert spec == SPEC
        assert {u.stage for u in updates} == {"verifying"}
        assert "Running code cell 2 of 2…" in [u.message for u in updates]

    def test_check_events_for_the_panel(self):
        provider = RepairProvider(FIX)
        _, _, updates = self._verify(provider, FakeRunner(3))
        checks = [u.check for u in updates if u.check]
        plan = checks[0]
        assert plan["type"] == "plan"
        assert (plan["attempt"], plan["max_attempts"]) == (1, 3)
        assert [c["label"] for c in plan["cells"]] == [
            "Install",
            "QUICK_RUN",
            "Cell 1",
            "Cell 2",
        ]
        assert plan["cells"][3]["preview"] == "print(stepz)"
        cells = [
            (c["index"], c["status"]) for c in checks if c["type"] == "cell"
        ]
        # Attempt 1 fails at index 3; attempt 2 passes every cell.
        assert cells[:8] == [
            (0, "running"),
            (0, "ok"),
            (1, "running"),
            (1, "ok"),
            (2, "running"),
            (2, "ok"),
            (3, "running"),
            (3, "failed"),
        ]
        failed = next(c for c in checks if c.get("status") == "failed")
        assert "NameError" in failed["error"]
        repairs = [c for c in checks if c["type"] == "repair"]
        assert [r["status"] for r in repairs] == ["running", "fixed"]
        assert repairs[1]["cells"] == [2]
        assert repairs[1]["packages"] is None
        assert repairs[1]["explanation"] == "Typo in the variable name."
        assert [c["attempt"] for c in checks if c["type"] == "plan"] == [1, 2]

    def test_failed_repair_event(self):
        _, _, updates = self._verify(
            RepairProvider(ProviderError("down")), FakeRunner(3)
        )
        last = [u.check for u in updates if u.check][-1]
        assert last["status"] == "failed"
        assert "down" in last["message"]

    def test_package_fix_event(self):
        fix = {"explanation": "", "pip_packages": ["einops", "x"], "fixes": []}
        _, _, updates = self._verify(RepairProvider(fix), FakeRunner(3))
        fixed = next(
            u.check
            for u in updates
            if u.check and u.check.get("status") == "fixed"
        )
        assert fixed["packages"] == ["einops", "x"]

    def test_setup_event(self):
        def prepare(settings, notify):
            notify("Preparing the sandbox…")
            return sys.executable

        _, _, updates = self._verify(
            RepairProvider(), FakeRunner(), prepare=prepare
        )
        assert updates[0].check == {
            "type": "setup",
            "message": "Preparing the sandbox…",
        }

    def test_preview_skips_comments(self):
        assert verify_module._preview("# note\n\nx = 1") == "x = 1"
        assert verify_module._preview("# only a comment") == "# only a comment"
        assert verify_module._preview("") == ""

    def test_plan_sets_quick_run_and_installs_packages(self):
        runner = FakeRunner()
        spec = {
            **SPEC,
            "cells": [
                {"cell_type": "code", "source": "QUICK_RUN = False\nx = 1"}
            ],
        }
        self._verify(RepairProvider(), runner, spec=spec)
        cells = runner.runs[0]
        assert cells[0] == "!pip install -q einops"
        assert cells[1].endswith("QUICK_RUN = True")
        # The model's own QUICK_RUN line cannot switch the check off.
        assert cells[2] == "QUICK_RUN = True\nx = 1"

    def test_failure_is_repaired(self):
        provider = RepairProvider(FIX)
        # Index 3 in the plan: pip, QUICK_RUN, code 1, code 2.
        runner = FakeRunner(3)
        spec, report, updates = self._verify(provider, runner)
        assert report.status == "passed"
        assert report.repairs == 1
        assert report.repaired_cells == (2,)
        assert report.usage == {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        }
        assert spec["cells"][3]["source"] == "print(steps)"
        assert SPEC["cells"][3]["source"] == "print(stepz)"  # not modified
        assert runner.runs[1][3] == "print(steps)"

        call = provider.calls[0]
        assert call["schema"] is REPAIR_SCHEMA
        assert call["pdf"] is None
        assert "Code cell 2 failed" in call["prompt"]
        assert "print(stepz)" in call["prompt"]
        assert "NameError" in call["prompt"]

        stages = [u.stage for u in updates]
        assert "repairing" in stages
        assert "done" not in stages
        repairing = [u for u in updates if u.stage == "repairing"]
        assert all(u.usage is None for u in repairing)
        assert any(u.thinking == "Hmm" for u in repairing)

    def test_gives_up_after_max_repairs(self):
        provider = RepairProvider(
            FIX, {**FIX, "fixes": [{"cell": 2, "source": "print(stepss)"}]}
        )
        runner = FakeRunner(3, 3, 3)
        _, report, _ = self._verify(provider, runner, _settings(max_repairs=2))
        assert report.status == "failed"
        assert report.repairs == 2
        assert report.failed_cell == 2
        assert "NameError" in report.error
        assert len(runner.runs) == 3
        assert "still fails" in report.note()

    def test_no_repairs_allowed(self):
        _, report, _ = self._verify(
            RepairProvider(), FakeRunner(3), _settings(max_repairs=0)
        )
        assert report.status == "failed"
        assert report.repairs == 0

    def test_setup_cell_failure(self):
        _, report, _ = self._verify(
            RepairProvider(), FakeRunner(0), _settings(max_repairs=0)
        )
        assert report.failed_cell is None
        assert report.message.startswith("Setup failed")
        assert "the setup cell" in report.note()

    def test_provider_error_during_repair(self):
        provider = RepairProvider(ProviderError("rate limited"))
        _, report, _ = self._verify(provider, FakeRunner(3))
        assert report.status == "failed"
        assert "rate limited" in report.message

    @pytest.mark.parametrize(
        "reply",
        [
            "not a dict",
            {"fixes": [{"cell": 9, "source": "x"}]},
            {"fixes": [{"cell": True, "source": "x"}]},
            {"fixes": [{"cell": 2, "source": "print(stepz)"}]},
            {"fixes": ["junk"], "pip_packages": ["einops"]},
        ],
    )
    def test_fix_that_changes_nothing(self, reply):
        _, report, _ = self._verify(RepairProvider(reply), FakeRunner(3))
        assert report.status == "failed"
        assert "suggested no change" in report.message

    def test_package_only_fix(self):
        fix = {"explanation": "", "pip_packages": ["einops", "x"], "fixes": []}
        spec, report, _ = self._verify(RepairProvider(fix), FakeRunner(3))
        assert report.status == "passed"
        assert report.repaired_cells == ()
        assert spec["pip_packages"] == ["einops", "x"]

    def test_skips_gpu_notebooks(self):
        runner = FakeRunner()
        _, report, _ = self._verify(
            RepairProvider(), runner, spec={**SPEC, "requires_gpu": True}
        )
        assert report.status == "skipped"
        assert "GPU" in report.message
        assert report.note() is None
        assert runner.runs == []

    def test_gpu_notebook_runs_when_allowed(self):
        _, report, _ = self._verify(
            RepairProvider(),
            FakeRunner(),
            _settings(skip_gpu=False),
            spec={**SPEC, "requires_gpu": True},
        )
        assert report.status == "passed"

    def test_skips_without_code(self):
        spec = {**SPEC, "cells": [SPEC["cells"][0]]}
        _, report, _ = self._verify(RepairProvider(), FakeRunner(), spec=spec)
        assert report.status == "skipped"

    def test_sandbox_error(self):
        def broken(settings, notify):
            raise SandboxError("no python")

        _, report, _ = self._verify(
            RepairProvider(), FakeRunner(), prepare=broken
        )
        assert report.status == "error"
        assert "no python" in report.message

    def test_real_run_end_to_end(self):
        spec = {
            **SPEC,
            "pip_packages": [],
            "cells": [{"cell_type": "code", "source": "assert QUICK_RUN"}],
        }
        _, report = verify_notebook(spec, RepairProvider(), _settings())
        assert report.status == "passed"

    def test_report_note_and_dict(self):
        passed = VerifyReport("passed", "ok", code_cells=3, repairs=2)
        assert "after 2 automatic fixes" in passed.note()
        assert "fix" not in VerifyReport("passed", "ok").note()
        assert VerifyReport("error", "x").note() is None
        assert passed.to_dict()["repairs"] == 2


class TestConverter:
    def test_verify_result_in_notebook(self):
        reply = {
            "title": "Tiny",
            "pip_packages": [],
            "requires_gpu": False,
            "cells": [
                {"cell_type": "code", "source": "x = 1 if QUICK_RUN else 2"}
            ],
        }
        provider = RepairProvider(reply)
        result = pdf_to_notebook(PDF, provider, verify=_settings())
        assert result.verification.status == "passed"
        assert (
            "Checked: every code cell ran" in result.notebook.cells[0].source
        )

    def test_invalid_reply_is_rejected_before_running(self):
        provider = RepairProvider({"title": "x", "cells": []})
        with pytest.raises(ValueError, match="no cells"):
            pdf_to_notebook(PDF, provider, verify=_settings())

    def test_without_verify(self):
        provider = RepairProvider(
            {**SPEC, "cells": [{"cell_type": "code", "source": "x"}]}
        )
        assert pdf_to_notebook(PDF, provider).verification is None


REPLY = {
    "title": "Tiny",
    "pip_packages": [],
    "requires_gpu": False,
    "cells": [{"cell_type": "code", "source": "print(1 if QUICK_RUN else 2)"}],
}


class TestEndpoints:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        def use(*, allow_remote=True, default=False, reply=REPLY):
            config = tmp_path / "verify.toml"
            config.write_text(
                "[verify]\n"
                f"allow_remote = {str(allow_remote).lower()}\n"
                f"default = {str(default).lower()}\n"
                f"python = {json.dumps(sys.executable)}\n"
            )
            monkeypatch.setenv("PAPER_TO_NOTEBOOK_CONFIG", str(config))
            api_main.app.dependency_overrides[api_main.get_llm_provider] = (
                lambda: RepairProvider(dict(reply), dict(reply))
            )
            return TestClient(api_main.app)

        yield use
        api_main.app.dependency_overrides.clear()

    def _post(self, client, path="/convert/stream", **data):
        return client.post(
            path,
            files={"file": ("paper.pdf", PDF, "application/pdf")},
            data=data,
        )

    def test_stream_result_has_verification(self, client):
        from test_progress import _parse_sse

        response = self._post(client(), verify="true")
        events = _parse_sse(response.text)
        stages = [d["stage"] for e, d in events if e == "progress"]
        assert "verifying" in stages
        assert stages[-1] == "building"
        kind, result = events[-1]
        assert kind == "result"
        assert result["verification"]["status"] == "passed"

    def test_stream_without_verify(self, client):
        from test_progress import _parse_sse

        events = _parse_sse(self._post(client()).text)
        assert events[-1][1]["verification"] is None

    def test_default_from_config(self, client):
        from test_progress import _parse_sse

        events = _parse_sse(self._post(client(default=True)).text)
        assert events[-1][1]["verification"]["status"] == "passed"

    def test_remote_client_is_refused(self, client):
        response = self._post(client(allow_remote=False), verify="true")
        assert response.status_code == 403
        assert "localhost" in response.json()["detail"]

    def test_remote_default_is_ignored(self, client):
        from test_progress import _parse_sse

        test_client = client(allow_remote=False, default=True)
        events = _parse_sse(self._post(test_client).text)
        assert events[-1][1]["verification"] is None

    def test_convert_sets_header(self, client):
        response = self._post(client(), path="/convert", verify="true")
        assert response.status_code == 200
        assert response.headers["x-notebook-verification"] == "passed"

    def test_config_describes_verify(self, client):
        info = client().get("/api/config").json()["verify"]
        assert info["available"] is True
        assert info["reason"] is None
        remote = client(allow_remote=False).get("/api/config").json()
        assert remote["verify"]["available"] is False
        assert "this machine" in remote["verify"]["reason"]

    def test_config_with_broken_override(self, monkeypatch, tmp_path):
        from api.routes.config import _describe_verify

        monkeypatch.setenv("PAPER_TO_NOTEBOOK_CONFIG", str(tmp_path / "x"))
        assert _describe_verify("127.0.0.1")["available"] is False

    def test_repair_tokens_count_in_metrics(self, client):
        from test_progress import _parse_sse

        reply = {
            **REPLY,
            "cells": [{"cell_type": "code", "source": "print(missing)"}],
        }
        # The fake provider answers the repair with the notebook reply,
        # which fixes nothing, so the check fails after one repair
        # request. Both requests report 15 tokens.
        events = _parse_sse(
            self._post(client(reply=reply), verify="true").text
        )
        result = events[-1][1]
        assert result["verification"]["status"] == "failed"
        assert result["usage"]["total_tokens"] == 30
