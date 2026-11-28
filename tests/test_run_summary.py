"""Tests for the per-run summaries: the E2E summary printed by ./e2e.sh
and the JSON report the tests write for it."""

import json

from tests.conftest import run_report
from tests.e2e import summary

REAL = "tests/e2e/test_dashboard.py::test_upload_example_pdf_with_real_model"
ARXIV = "tests/e2e/test_dashboard.py::test_arxiv_link_with_faked_mcp"
PAGE = "tests/e2e/test_dashboard.py::test_tabs_switch_panels"
NOTES = {
    "label": "upload",
    "provider": "anthropic",
    "model": "claude-sonnet-5-5",
    "tokens": "8,028",
    "cost": "$0.04",
    "cells": 13,
}


def _report(outcomes, notes=None, reasons=None):
    types = {nodeid: ["e2e"] for nodeid in outcomes}
    durations = {nodeid: 1.25 for nodeid in outcomes}
    return run_report(types, outcomes, durations, reasons or {}, notes or {})


class TestRunReport:
    def test_plain_data_in_run_order(self):
        report = _report(
            {PAGE: "passed", REAL: "skipped"},
            reasons={REAL: "no real provider"},
        )
        assert json.loads(json.dumps(report)) == report
        assert [t["id"] for t in report["tests"]] == [PAGE, REAL]
        assert report["tests"][0]["seconds"] == 1.2
        assert report["tests"][1]["reason"] == "no real provider"
        assert report["summary"].startswith("Tests by type: e2e 2")


class TestE2ESummary:
    def test_conversion_line(self):
        assert summary.conversion_line(NOTES) == (
            "provider : anthropic  model: claude-sonnet-5-5: 8,028 tokens,"
            " cost $0.04, 13 cells"
        )

    def test_totals_add_up(self):
        report = _report(
            {REAL: "passed", ARXIV: "passed"},
            notes={REAL: NOTES, ARXIV: {**NOTES, "tokens": "1,000"}},
        )
        assert summary.totals_line(report["tests"]) == (
            "Real model: 2 conversion(s), 9,028 tokens, cost $0.08"
        )

    def test_unpriced_model(self):
        report = _report(
            {REAL: "passed"}, notes={REAL: {**NOTES, "cost": "—"}}
        )
        assert "cost not priced" in summary.totals_line(report["tests"])

    def test_no_conversions(self):
        assert "no conversions ran" in summary.totals_line([])

    def test_render_without_colour(self):
        report = _report(
            {PAGE: "passed", REAL: "passed", ARXIV: "skipped"},
            notes={REAL: NOTES},
            reasons={ARXIV: "no real provider"},
        )
        text = "\n".join(
            summary.render(report, "anthropic", "claude-sonnet-5-5")
        )
        assert "\033[" not in text
        assert "provider : anthropic  model: claude-sonnet-5-5" in text
        assert "8,028 tokens, cost $0.04, 13 cells" in text
        assert "skipped: no real provider" in text
        assert "Test summary: 3 tests: 2 passed, 1 skipped" in text
        assert "real model" in text and "scripted" in text

    def test_render_colours_by_outcome(self):
        report = _report({PAGE: "passed", REAL: "failed"})
        lines = summary.render(report, "x", "y", colour=True)
        assert lines[0].startswith("\033[31;1m")  # failed run: red frame
        assert any(line.startswith("\033[32;1mpassed") for line in lines)
        assert any(line.startswith("\033[31;1mfailed") for line in lines)

    def test_main(self, tmp_path, capsys):
        path = tmp_path / "report.json"
        path.write_text(json.dumps(_report({PAGE: "passed"})))
        assert summary.main([str(path), "anthropic", "m"]) == 0
        assert "E2E summary" in capsys.readouterr().out

    def test_main_without_results(self, tmp_path, capsys):
        assert summary.main([str(tmp_path / "none.json"), "a", "m"]) == 1
        assert "did not run" in capsys.readouterr().out
