"""Shared test setup."""

import json
import os
from collections import Counter

import pytest

LEVELS = {"unit", "integration", "e2e"}
# Order of the per-type summary line (security is on top of a level).
TYPES = ("unit", "integration", "e2e", "security")

_types = {}  # test id -> its type markers
_outcomes = {}  # test id -> "passed" / "failed" / "skipped"
_durations = Counter()  # test id -> seconds (setup + call + teardown)
_reasons = {}  # test id -> why it was skipped
# Extra facts a test reports for the run summary (e.g. the E2E tests'
# provider, model, tokens and cost), by test id. See ``run_note``.
RUN_NOTES = {}
# When set, the run's results are written to this JSON file (./e2e.sh).
REPORT_ENV_VAR = "TEST_REPORT_JSON"


def pytest_collection_modifyitems(items):
    """Mark every test without a level marker as a unit test."""
    for item in items:
        names = {mark.name for mark in item.iter_markers()}
        if not LEVELS & names:
            item.add_marker(pytest.mark.unit)
            names.add("unit")
        _types[item.nodeid] = [t for t in TYPES if t in names]


def pytest_runtest_logreport(report):
    """Remember each test's outcome (a failure in any phase wins)."""
    _durations[report.nodeid] += report.duration
    if report.skipped and isinstance(report.longrepr, tuple):
        _reasons[report.nodeid] = report.longrepr[2].removeprefix("Skipped: ")
    if report.failed:
        _outcomes[report.nodeid] = "failed"
    elif report.skipped:
        _outcomes.setdefault(report.nodeid, "skipped")
    elif report.when == "call":
        _outcomes.setdefault(report.nodeid, "passed")


def type_summary(types, outcomes):
    """One line: how many tests of each type ran and how they ended."""
    counts = {name: Counter() for name in TYPES}
    for nodeid, outcome in outcomes.items():
        for name in types.get(nodeid, ()):
            counts[name][outcome] += 1
    parts = []
    for name in TYPES:
        count = counts[name]
        if not count:
            continue
        detail = ", ".join(
            f"{count[o]} {o}"
            for o in ("passed", "failed", "skipped")
            if count[o]
        )
        parts.append(f"{name} {sum(count.values())} ({detail})")
    return "Tests by type: " + ("; ".join(parts) or "none ran")


def pytest_terminal_summary(terminalreporter):
    """Print the per-type line; ./check.sh copies it into its summary."""
    terminalreporter.write_line(type_summary(_types, _outcomes))


@pytest.fixture
def run_note(request):
    """Record facts about this test for the run summary.

    ``run_note(model="…", tokens="8,028")`` adds to this test's notes;
    ./e2e.sh prints them after the run.
    """

    def note(**facts):
        RUN_NOTES.setdefault(request.node.nodeid, {}).update(facts)

    return note


def run_report(types, outcomes, durations, reasons, notes):
    """The run's results as plain data, in the order the tests ran."""
    return {
        "summary": type_summary(types, outcomes),
        "tests": [
            {
                "id": nodeid,
                "outcome": outcome,
                "types": types.get(nodeid, []),
                "seconds": round(durations.get(nodeid, 0.0), 1),
                "reason": reasons.get(nodeid, ""),
                "notes": notes.get(nodeid, {}),
            }
            for nodeid, outcome in outcomes.items()
        ],
    }


def pytest_sessionfinish(session):
    """Write the results to $TEST_REPORT_JSON, if set (./e2e.sh)."""
    path = os.environ.get(REPORT_ENV_VAR)
    if path:
        report = run_report(_types, _outcomes, _durations, _reasons, RUN_NOTES)
        with open(path, "w", encoding="utf-8") as file:
            json.dump(report, file, indent=2)


@pytest.fixture(autouse=True)
def notebook_output_dir(tmp_path, monkeypatch):
    """Save generated notebooks to a temp folder, not the real output/."""
    directory = tmp_path / "output"
    monkeypatch.setenv("NOTEBOOK_OUTPUT_DIR", str(directory))
    return directory


@pytest.fixture(autouse=True)
def allow_test_host(monkeypatch):
    """FastAPI's TestClient sends Host: testserver; allow it in tests only.

    The real allowed hosts (loopback names plus [app] allowed_hosts) are
    tested in test_security.py.
    """
    import api.security as security

    real = security.load_allowed_hosts
    monkeypatch.setattr(
        security, "allowed_hosts", lambda: real() | {"testserver"}
    )
