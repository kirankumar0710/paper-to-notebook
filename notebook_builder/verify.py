"""Optional check that a generated notebook runs, with LLM repairs.

After the model writes the notebook, its code cells are run top to bottom
in a separate Python process (:mod:`notebook_builder.sandbox_runner`) with
``QUICK_RUN = True``, so training loops and datasets are cut down to a
smoke test. If a cell fails or times out, the code and the error go back
to the model for a fix, and the notebook is run again, up to
``max_repairs`` times.

Repairs use only :meth:`providers.base.LLMProvider.generate_json`, the
interface every provider implements, so the check works the same with
Anthropic, Gemini and OpenAI.

The code is written by an LLM, so it runs with a minimal environment (no
API keys), in a throwaway working folder, under per-cell and total
timeouts. Settings come from ``[verify]`` in config.toml.
"""

from __future__ import annotations

import copy
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import venv
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from notebook_builder.builder import pip_install_source, quick_run_source
from providers.base import (
    LLMProvider,
    Progress,
    ProgressCallback,
    ProviderError,
)
from util.config import Config, load_config
from util.log import get_logger

__all__ = [
    "LOCAL_HOSTS",
    "PROTOCOL_PREFIX",
    "REPAIR_SCHEMA",
    "RunResult",
    "SandboxError",
    "VerifyReport",
    "VerifySettings",
    "load_verify_settings",
    "run_cells",
    "sandbox_python",
    "verify_allowed",
    "verify_notebook",
]

logger = get_logger(__name__)

SECTION = "verify"
RUNNER = Path(__file__).with_name("sandbox_runner.py")
# Keep in sync with sandbox_runner.PREFIX.
PROTOCOL_PREFIX = "@@p2n-verify@@ "
LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
# How much of an error or traceback is kept for the report and the prompt.
MAX_ERROR_CHARS = 4000
# Lines of non-protocol output kept to explain a crashed runner.
TAIL_LINES = 40
DEFAULT_BASE_PACKAGES = (
    "numpy",
    "scipy",
    "pandas",
    "matplotlib",
    "scikit-learn",
    "torch",
)
# A model cell that sets QUICK_RUN itself would undo the check's setting.
_QUICK_RUN_ASSIGNMENT = re.compile(r"^QUICK_RUN\s*=.*$", re.MULTILINE)


class SandboxError(RuntimeError):
    """The sandbox interpreter could not be found or set up."""


@dataclass(frozen=True)
class VerifySettings:
    """``[verify]`` from config.toml; see the comments there."""

    default: bool = False
    allow_remote: bool = False
    skip_gpu: bool = True
    cell_timeout: float = 120.0
    total_timeout: float = 600.0
    max_repairs: int = 2
    python: str = ""
    sandbox_dir: Path = field(
        default_factory=lambda: (
            Path.home() / ".cache" / "paper-to-notebook" / "verify-env"
        )
    )
    base_packages: tuple[str, ...] = DEFAULT_BASE_PACKAGES
    setup_timeout: float = 1800.0

    @property
    def pip_cache(self) -> Path:
        """Kept between runs, so packages are downloaded once."""
        return self.sandbox_dir / "pip-cache"


def load_verify_settings(config: Config | None = None) -> VerifySettings:
    """Read ``[verify]``; invalid values fall back to the defaults."""
    if config is None:
        config = load_config()
    section = config.get(SECTION, {})
    defaults = VerifySettings()
    values: dict[str, Any] = {}

    def pick(key: str, kind: type | tuple[type, ...], check=None) -> None:
        if key not in section:
            return
        value = section[key]
        ok = isinstance(value, kind) and not (
            kind is not bool and isinstance(value, bool)
        )
        if ok and (check is None or check(value)):
            values[key] = value
        else:
            logger.warning("Ignoring invalid [verify] %s = %r", key, value)

    for key in ("default", "allow_remote", "skip_gpu"):
        pick(key, bool)
    for key in ("cell_timeout", "total_timeout", "setup_timeout"):
        pick(key, (int, float), lambda v: v > 0)
    pick("max_repairs", int, lambda v: 0 <= v <= 10)
    pick("python", str)
    pick("sandbox_dir", str, lambda v: bool(v.strip()))
    pick(
        "base_packages",
        list,
        lambda v: all(isinstance(p, str) and p.strip() for p in v),
    )
    if "sandbox_dir" in values:
        values["sandbox_dir"] = Path(
            os.path.expandvars(values["sandbox_dir"])
        ).expanduser()
    if "python" in values:
        values["python"] = os.path.expanduser(values["python"].strip())
    if "base_packages" in values:
        values["base_packages"] = tuple(
            p.strip() for p in values["base_packages"]
        )
    for key in ("cell_timeout", "total_timeout", "setup_timeout"):
        if key in values:
            values[key] = float(values[key])
    return replace(defaults, **values)


def verify_allowed(client_host: str | None, settings: VerifySettings) -> bool:
    """Whether the check may run for a request from ``client_host``."""
    return settings.allow_remote or client_host in LOCAL_HOSTS


# ---------------------------------------------------------------------------
# Sandbox interpreter
# ---------------------------------------------------------------------------

_setup_lock = threading.Lock()
_MARKER = ".p2n-packages"


def _venv_python(directory: Path) -> Path:
    if os.name == "nt":
        return directory / "Scripts" / "python.exe"
    return directory / "bin" / "python"


def sandbox_python(
    settings: VerifySettings, notify: Callable[[str], None] | None = None
) -> str:
    """Return the interpreter to run notebooks with, creating the sandbox
    virtual environment and installing ``base_packages`` on first use.

    Raises:
        SandboxError: If the interpreter is missing or setup fails.
    """
    if settings.python:
        if shutil.which(settings.python) is None:
            raise SandboxError(
                f"[verify] python = {settings.python!r} was not found."
            )
        return settings.python
    directory = settings.sandbox_dir
    python = _venv_python(directory)
    marker = directory / _MARKER
    wanted = "\n".join(sorted(settings.base_packages))
    with _setup_lock:
        if (
            python.exists()
            and marker.is_file()
            and marker.read_text() == wanted
        ):
            return str(python)
        if notify is not None:
            notify(
                "Preparing the sandbox (first run only): installing "
                + (", ".join(settings.base_packages) or "nothing")
                + "…"
            )
        logger.info("Setting up the verify sandbox in %s", directory)
        try:
            if not python.exists():
                venv.create(directory, with_pip=True)
            if settings.base_packages:
                # Fixed arguments: our interpreter and configured packages.
                subprocess.run(  # noqa: S603
                    [
                        str(python),
                        "-m",
                        "pip",
                        "install",
                        "-q",
                        *settings.base_packages,
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=settings.setup_timeout,
                    env={
                        **os.environ,
                        "PIP_CACHE_DIR": str(settings.pip_cache),
                    },
                )
            marker.write_text(wanted)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()[-500:]
            raise SandboxError(
                f"Installing the sandbox packages failed: {detail}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise SandboxError(
                "Installing the sandbox packages took longer than "
                f"{settings.setup_timeout:.0f} s."
            ) from exc
        except OSError as exc:
            raise SandboxError(f"Could not create the sandbox: {exc}") from exc
    return str(python)


# ---------------------------------------------------------------------------
# Running cells
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunResult:
    """One run of the notebook.

    Attributes:
        ok: Every cell finished.
        failed: Index (into the cells passed in) of the cell that failed.
        error: One-line error, e.g. ``"NameError: name 'x' is not defined"``.
        details: Traceback and the cell's last output, for the model.
        timed_out: The cell, or the whole run, hit its timeout.
    """

    ok: bool
    failed: int | None = None
    error: str = ""
    details: str = ""
    timed_out: bool = False


def _sandbox_env(python: str, workdir: str, settings: VerifySettings):
    """A minimal environment: no API keys or other secrets."""
    env = {
        "PATH": os.pathsep.join(
            [str(Path(python).parent), os.environ.get("PATH", os.defpath)]
        ),
        "HOME": workdir,
        "TMPDIR": workdir,
        "MPLBACKEND": "Agg",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_CACHE_DIR": str(settings.pip_cache),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "LANG": "C.UTF-8",
    }
    for name in ("SYSTEMROOT", "WINDIR"):  # Windows needs these
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def _kill(process: subprocess.Popen[str]) -> None:
    """Stop the runner and anything it started."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


CellEvent = dict[str, Any]


def run_cells(
    python: str,
    cells: list[str],
    settings: VerifySettings,
    on_event: Callable[[CellEvent], None] | None = None,
) -> RunResult:
    """Run ``cells`` in order with ``python``; stop at the first failure.

    Args:
        python: Interpreter to run them with.
        cells: Cell sources, in order.
        settings: Timeouts and the pip cache.
        on_event: Called with ``{"event": "start", "cell": i}`` as each
            cell starts and ``{"event": "ok", "cell": i, "seconds": s,
            "output": text}`` as it finishes. Failures are in the result.
    """
    workdir = tempfile.mkdtemp(prefix="p2n-verify-")
    try:
        # Fixed arguments: the configured interpreter and our runner.
        process = subprocess.Popen(  # noqa: S603
            [python, str(RUNNER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=workdir,
            env=_sandbox_env(python, workdir, settings),
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        return RunResult(False, error=f"Could not start {python}: {exc}")

    lines: queue.Queue[str | None] = queue.Queue()

    def read() -> None:
        assert process.stdout is not None  # noqa: S101 (type narrowing)
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        assert process.stdin is not None  # noqa: S101 (type narrowing)
        process.stdin.write(json.dumps({"cells": cells}))
        process.stdin.close()
    except OSError:
        pass  # the runner died at once; reported below

    try:
        return _watch(process, lines, settings, on_event)
    finally:
        _kill(process)
        process.wait()
        reader.join(timeout=5)
        shutil.rmtree(workdir, ignore_errors=True)


def _watch(
    process: subprocess.Popen[str],
    lines: queue.Queue[str | None],
    settings: VerifySettings,
    on_event: Callable[[CellEvent], None] | None,
) -> RunResult:
    """Follow the runner's protocol lines and enforce the timeouts."""
    run_deadline = time.monotonic() + settings.total_timeout
    cell_deadline: float | None = None
    cell_started = 0.0
    current: int | None = None
    tail: list[str] = []
    while True:
        deadline = run_deadline
        if cell_deadline is not None:
            deadline = min(deadline, cell_deadline)
        try:
            line = lines.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            whole_run = cell_deadline is None or run_deadline <= cell_deadline
            limit = (
                f"the whole notebook took over {settings.total_timeout:.0f} s"
                if whole_run
                else f"it ran for over {settings.cell_timeout:.0f} s"
            )
            return RunResult(
                False,
                failed=current,
                error=f"TimeoutError: {limit}",
                details=(
                    f"The cell was stopped because {limit}. With QUICK_RUN "
                    "= True it must finish quickly: use less data and "
                    "fewer epochs or iterations.\n" + "".join(tail[-10:])
                ),
                timed_out=True,
            )
        if line is None:
            break
        if not line.startswith(PROTOCOL_PREFIX):
            tail = [*tail[-(TAIL_LINES - 1) :], line]
            continue
        message = json.loads(line[len(PROTOCOL_PREFIX) :])
        event = message.get("event")
        if event == "start":
            current = message["cell"]
            cell_started = time.monotonic()
            cell_deadline = cell_started + settings.cell_timeout
            if on_event is not None:
                on_event({"event": "start", "cell": current})
        elif event == "ok":
            cell_deadline = None
            if on_event is not None:
                on_event(
                    {
                        "event": "ok",
                        "cell": message["cell"],
                        "seconds": round(time.monotonic() - cell_started, 2),
                        "output": message.get("output", ""),
                    }
                )
        elif event == "error":
            details = message.get("traceback", "")
            output = message.get("output", "").strip()
            if output:
                details += f"\nOutput before the error:\n{output}"
            return RunResult(
                False,
                failed=message["cell"],
                error=message.get("error", "error"),
                details=details[-MAX_ERROR_CHARS:],
            )
        elif event == "finished":
            return RunResult(True)
    process.wait()
    return RunResult(
        False,
        failed=current,
        error=f"The Python process exited with code {process.returncode}",
        details="".join(tail)[-MAX_ERROR_CHARS:],
    )


# ---------------------------------------------------------------------------
# Verify and repair
# ---------------------------------------------------------------------------

REPAIR_SYSTEM = """\
You fix bugs in Google Colab notebooks that were generated from research \
papers. Change as little as possible: keep the explanations, the paper's \
method and the structure, and only correct what stops the code running. \
Do not define QUICK_RUN; it is set in an earlier cell.
"""

REPAIR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "explanation": {
            "type": "string",
            "description": "One or two sentences: what was wrong.",
        },
        "pip_packages": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "The complete list of PyPI packages to install that are "
                "not preinstalled on Colab."
            ),
        },
        "fixes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "cell": {
                        "type": "integer",
                        "description": "Code cell number, as in the prompt.",
                    },
                    "source": {
                        "type": "string",
                        "description": "The cell's complete new source.",
                    },
                },
                "required": ["cell", "source"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["explanation", "pip_packages", "fixes"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class VerifyReport:
    """What the check found.

    Attributes:
        status: ``passed``, ``failed``, ``skipped`` or ``error`` (the
            sandbox could not be set up).
        message: One line for the dashboard.
        code_cells: Code cells written by the model.
        failed_cell: Code cell number (1-based) that failed last, if any.
        error: The last error, one line.
        repairs: Fixes the model made.
        repaired_cells: Code cell numbers the model changed.
        elapsed: Seconds spent checking and repairing.
        usage: Tokens used by the repair requests, if any.
    """

    status: str
    message: str
    code_cells: int = 0
    failed_cell: int | None = None
    error: str | None = None
    repairs: int = 0
    repaired_cells: tuple[int, ...] = ()
    elapsed: float = 0.0
    usage: dict[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "code_cells": self.code_cells,
            "failed_cell": self.failed_cell,
            "error": self.error,
            "repairs": self.repairs,
            "repaired_cells": list(self.repaired_cells),
            "elapsed": round(self.elapsed, 1),
            "usage": self.usage,
        }

    def note(self) -> str | None:
        """A line for the notebook's title cell, or None."""
        if self.status == "passed":
            fixed = (
                f" after {self.repairs} automatic "
                f"fix{'es' if self.repairs != 1 else ''}"
                if self.repairs
                else ""
            )
            return (
                "*Checked: every code cell ran without errors with "
                f"`QUICK_RUN = True`{fixed}.*"
            )
        if self.status == "failed":
            where = (
                f"code cell {self.failed_cell}"
                if self.failed_cell
                else "the setup cell"
            )
            return (
                f"*Checked with `QUICK_RUN = True`: {where} still fails "
                f"(`{self.error}`). Review it before running.*"
            )
        return None


def _code_cells(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        cell
        for cell in spec.get("cells") or []
        if isinstance(cell, dict) and cell.get("cell_type") == "code"
    ]


def _plan(spec: dict[str, Any]) -> tuple[list[str], list[int | None]]:
    """The sources to run and, for each, its code cell number (None for the
    cells the builder adds: pip install and QUICK_RUN)."""
    sources: list[str] = []
    numbers: list[int | None] = []
    install = pip_install_source(spec.get("pip_packages") or [])
    if install is not None:
        sources.append(install)
        numbers.append(None)
    sources.append(quick_run_source(True))
    numbers.append(None)
    for number, cell in enumerate(_code_cells(spec), start=1):
        sources.append(
            _QUICK_RUN_ASSIGNMENT.sub("QUICK_RUN = True", cell["source"])
        )
        numbers.append(number)
    return sources, numbers


def _repair_prompt(
    spec: dict[str, Any],
    failed: int | None,
    result: RunResult,
    settings: VerifySettings,
) -> str:
    packages = ", ".join(spec.get("pip_packages") or []) or "none"
    where = f"Code cell {failed}" if failed else "The pip install cell"
    cells = "\n\n".join(
        f"### Code cell {number}\n```python\n{cell['source']}\n```"
        for number, cell in enumerate(_code_cells(spec), start=1)
    )
    return (
        f'The notebook "{spec.get("title", "")}" failed a quick check. '
        "Its code cells were run top to bottom with QUICK_RUN = True; "
        "with QUICK_RUN True every cell must finish within "
        f"{settings.cell_timeout:.0f} seconds on a CPU.\n\n"
        f"Extra pip packages installed: {packages}. Packages that Google "
        "Colab preinstalls (numpy, scipy, pandas, matplotlib, "
        "scikit-learn, torch, ...) are available.\n\n"
        f"{where} failed with:\n```\n{result.error}\n{result.details}\n```"
        f"\n\nThe code cells:\n\n{cells}\n\n"
        "Return in `fixes` the complete new source of each code cell you "
        "change, using the numbers above, and in `pip_packages` the "
        "complete package list."
    )


def _apply_fix(spec: dict[str, Any], fix: Any) -> tuple[int, ...]:
    """Apply the model's fix to ``spec`` in place.

    Returns:
        The code cell numbers that changed, plus 0 if the package list
        changed; empty if the fix changed nothing.
    """
    if not isinstance(fix, dict):
        return ()
    changed: list[int] = []
    code = _code_cells(spec)
    for item in fix.get("fixes") or []:
        if not isinstance(item, dict):
            continue
        number, source = item.get("cell"), item.get("source")
        if (
            isinstance(number, int)
            and not isinstance(number, bool)
            and 1 <= number <= len(code)
            and isinstance(source, str)
            and source.strip()
            and source != code[number - 1]["source"]
        ):
            code[number - 1]["source"] = source
            changed.append(number)
    packages = fix.get("pip_packages")
    if (
        isinstance(packages, list)
        and all(isinstance(p, str) for p in packages)
        and packages != list(spec.get("pip_packages") or [])
    ):
        spec["pip_packages"] = packages
        changed.append(0)
    return tuple(changed)


def _add_usage(total: dict[str, int], usage: dict[str, int]) -> None:
    for key, value in usage.items():
        total[key] = total.get(key, 0) + value


Runner = Callable[
    [str, list[str], VerifySettings, Callable[[CellEvent], None] | None],
    RunResult,
]


def _preview(source: str) -> str:
    """The first meaningful line of a cell, for the Run check panel."""
    for line in source.splitlines():
        text = line.strip()
        if text and not text.startswith("#"):
            return text[:120]
    return source.strip().splitlines()[0][:120] if source.strip() else ""


def _plan_cells(
    sources: list[str], numbers: list[int | None]
) -> list[dict[str, Any]]:
    """Describe the cells of one run for the Run check panel."""
    cells = []
    for source, number in zip(sources, numbers, strict=True):
        if number is not None:
            label = f"Cell {number}"
        elif source.startswith("!pip"):
            label = "Install"
        else:
            label = "QUICK_RUN"
        cells.append(
            {
                "label": label,
                "number": number,
                "preview": _preview(source),
                "source": source,
            }
        )
    return cells


Prepare = Callable[[VerifySettings, Callable[[str], None] | None], str]


def verify_notebook(
    spec: dict[str, Any],
    provider: LLMProvider,
    settings: VerifySettings,
    *,
    on_progress: ProgressCallback | None = None,
    runner: Runner = run_cells,
    prepare: Prepare = sandbox_python,
) -> tuple[dict[str, Any], VerifyReport]:
    """Run the notebook described by ``spec`` and have ``provider`` fix it.

    Args:
        spec: The model's reply (``NOTEBOOK_SCHEMA``). Not modified.
        provider: The LLM that wrote it; used for repairs.
        settings: ``[verify]`` settings.
        on_progress: Receives ``verifying`` and ``repairing`` updates.
            Each update for the Run check panel carries a ``check`` event:

            - ``{"type": "setup", "message"}`` while the sandbox is made
            - ``{"type": "plan", "attempt", "max_attempts", "cells"}`` at
              the start of each run; each cell has ``label``, ``number``
              (None for the install and QUICK_RUN cells), ``preview`` and
              ``source``
            - ``{"type": "cell", "attempt", "index", "status"}`` with
              ``status`` ``running``; ``ok`` plus ``seconds`` and
              ``output``; or ``failed`` plus ``error``, ``details`` and
              ``timed_out``
            - ``{"type": "repair", "attempt", "status"}`` with ``status``
              ``running``; ``fixed`` plus ``explanation``, ``cells`` and
              ``packages`` (the new list, or None if unchanged); or
              ``failed`` plus ``message``
        runner: Runs the cells (replaced in tests).
        prepare: Returns the interpreter (replaced in tests).

    Returns:
        The spec, with any repairs applied, and the report.
    """
    started = time.monotonic()
    spec = copy.deepcopy(spec)
    count = len(_code_cells(spec))

    def notify(
        stage: str, message: str, check: dict[str, Any] | None = None
    ) -> None:
        if check is None or check.get("type") != "cell":
            logger.info("Verify: %s", message)
        if on_progress is not None:
            on_progress(
                Progress(
                    stage,
                    message,
                    elapsed=time.monotonic() - started,
                    check=check,
                )
            )

    def report(status: str, message: str, **extra: Any) -> VerifyReport:
        result = VerifyReport(
            status,
            message,
            code_cells=count,
            elapsed=time.monotonic() - started,
            **extra,
        )
        logger.info("Verify result: %s", result.to_dict())
        return result

    if count == 0:
        return spec, report("skipped", "No code cells to run.")
    if settings.skip_gpu and spec.get("requires_gpu"):
        return spec, report(
            "skipped",
            "Not checked: the notebook needs a GPU. Run it in Colab with a "
            "GPU runtime.",
        )
    try:
        python = prepare(
            settings,
            lambda m: notify("verifying", m, {"type": "setup", "message": m}),
        )
    except SandboxError as exc:
        return spec, report("error", f"Could not check the notebook: {exc}")

    usage: dict[str, int] = {}
    repaired: set[int] = set()
    repairs = 0

    def forward(progress: Progress) -> None:
        """Pass a repair request's updates on as ``repairing``."""
        if progress.usage:
            _add_usage(usage, progress.usage)
        if on_progress is not None and progress.stage != "done":
            on_progress(
                replace(
                    progress,
                    stage="repairing",
                    message=f"Repairing: {progress.message}",
                    usage=None,
                    served=None,
                )
            )

    while True:
        sources, numbers = _plan(spec)
        attempt = repairs + 1
        after = f" (after fix {repairs})" if repairs else ""
        notify(
            "verifying",
            f"Running the notebook{after}…",
            {
                "type": "plan",
                "attempt": attempt,
                "max_attempts": settings.max_repairs + 1,
                "cells": _plan_cells(sources, numbers),
            },
        )

        def on_event(
            event: CellEvent, numbers=numbers, attempt=attempt
        ) -> None:
            index = event["cell"]
            number = numbers[index]
            if event["event"] == "start":
                notify(
                    "verifying",
                    f"Running code cell {number} of {count}…"
                    if number
                    else "Running the setup cells…",
                    {
                        "type": "cell",
                        "attempt": attempt,
                        "index": index,
                        "status": "running",
                    },
                )
            else:
                notify(
                    "verifying",
                    f"Code cell {number} ran in {event['seconds']:.1f} s."
                    if number
                    else "Setup cell done.",
                    {
                        "type": "cell",
                        "attempt": attempt,
                        "index": index,
                        "status": "ok",
                        "seconds": event["seconds"],
                        "output": event.get("output", ""),
                    },
                )

        result = runner(python, sources, settings, on_event)
        failed = numbers[result.failed] if result.failed is not None else None
        if not result.ok:
            notify(
                "verifying",
                f"{'Code cell ' + str(failed) if failed else 'Setup'} "
                f"failed: {result.error}",
                {
                    "type": "cell",
                    "attempt": attempt,
                    "index": result.failed,
                    "status": "failed",
                    "error": result.error,
                    "details": result.details,
                    "timed_out": result.timed_out,
                },
            )
        extra = {
            "repairs": repairs,
            "repaired_cells": tuple(sorted(repaired)),
            "usage": usage or None,
        }
        if result.ok:
            plural = "es" if repairs != 1 else ""
            fixed = f" after {repairs} fix{plural}" if repairs else ""
            return spec, report(
                "passed",
                f"All {count} code cells ran without errors{fixed}.",
                **extra,
            )
        where = f"Code cell {failed}" if failed else "Setup"
        if repairs >= settings.max_repairs:
            return spec, report(
                "failed",
                f"{where} failed: {result.error}",
                failed_cell=failed,
                error=result.error,
                **extra,
            )
        notify(
            "repairing",
            f"{where} failed ({result.error}); asking "
            f"{provider.name} to fix it…",
            {"type": "repair", "attempt": attempt, "status": "running"},
        )

        def repair_failed(message: str, attempt=attempt) -> None:
            notify(
                "repairing",
                message,
                {
                    "type": "repair",
                    "attempt": attempt,
                    "status": "failed",
                    "message": message,
                },
            )

        try:
            fix = provider.generate_json(
                _repair_prompt(spec, failed, result, settings),
                schema=REPAIR_SCHEMA,
                system=REPAIR_SYSTEM,
                on_progress=forward,
            )
        except ProviderError as exc:
            repair_failed(f"The fix request failed: {exc}")
            return spec, report(
                "failed",
                f"{where} failed: {result.error} (the fix request failed: "
                f"{exc})",
                failed_cell=failed,
                error=result.error,
                **{**extra, "usage": usage or None},
            )
        old_packages = list(spec.get("pip_packages") or [])
        changed = _apply_fix(spec, fix)
        if not changed:
            repair_failed("The model suggested no change.")
            return spec, report(
                "failed",
                f"{where} failed: {result.error} (the model suggested no "
                "change)",
                failed_cell=failed,
                error=result.error,
                **{**extra, "usage": usage or None},
            )
        repairs += 1
        repaired.update(n for n in changed if n)
        explanation = str(fix.get("explanation", "")).strip()
        cells = [n for n in changed if n]
        packages = spec.get("pip_packages")
        notify(
            "repairing",
            f"Fix {repairs}: {explanation or 'changed ' + str(changed)}",
            {
                "type": "repair",
                "attempt": attempt,
                "status": "fixed",
                "explanation": explanation,
                "cells": cells,
                "packages": packages if packages != old_packages else None,
            },
        )
