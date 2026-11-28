"""Print the summary of an E2E run (``./e2e.sh`` calls this).

    python -m tests.e2e.summary REPORT_JSON PROVIDER MODEL

REPORT_JSON is the file the tests wrote (``TEST_REPORT_JSON``, see
``tests/conftest.py``). Green for passed, red for failed and yellow for
skipped, when the terminal shows colour (not with ``NO_COLOR``).
"""

import json
import os
import re
import sys
import textwrap

WIDTH = 81
COLOURS = {"passed": "32", "failed": "31", "skipped": "33"}


def number(text):
    """8028 from "8,028"; None when there is no number (e.g. "—")."""
    digits = re.sub(r"[^\d.]", "", text or "")
    return float(digits) if digits.strip(".") else None


def conversion_line(notes):
    """`provider : anthropic  model: claude-sonnet-5-5: 8,028 tokens, …`."""
    return (
        f"provider : {notes['provider']}  model: {notes['model']}:"
        f" {notes['tokens']} tokens, cost {notes['cost']},"
        f" {notes['cells']} cells"
    )


def totals_line(tests):
    """Tokens and cost added up over the real-model conversions."""
    runs = [t["notes"] for t in tests if "tokens" in t["notes"]]
    if not runs:
        return "Real model: no conversions ran (no provider or key)."
    tokens = [number(n["tokens"]) for n in runs]
    costs = [number(n["cost"]) for n in runs]
    total_tokens = (
        f"{int(sum(tokens)):,} tokens" if None not in tokens else "tokens ?"
    )
    total_cost = (
        f"cost ${sum(costs):.4f}".rstrip("0").rstrip(".")
        if None not in costs
        else "cost not priced"
    )
    return (
        f"Real model: {len(runs)} conversion(s), {total_tokens}, {total_cost}"
    )


def render(report, provider, model, colour=False):
    """The summary as lines of text."""

    def paint(text, outcome, bold=False):
        if not colour:
            return text
        code = COLOURS.get(outcome, "0") + (";1" if bold else "")
        return f"\033[{code}m{text}\033[0m"

    tests = report["tests"]
    failed = any(t["outcome"] == "failed" for t in tests)
    frame = "failed" if failed else "passed"
    title = " E2E summary "
    lines = [
        paint(f"{title:=^{WIDTH}}", frame, bold=True),
        paint(f"provider : {provider}  model: {model}", frame),
        "",
    ]
    for test in tests:
        name = test["id"].rsplit("::", 1)[-1]
        kind = "real model" if test["id"].endswith(REAL_TESTS) else "scripted"
        outcome = test["outcome"]
        head = f"{outcome:<7} {name:<52} {kind:<10} {test['seconds']:>5}s"
        lines.append(paint(head, outcome, bold=True))
        if test["notes"].get("tokens") is not None:
            # One line, never wrapped, so it can be copied as it is.
            line = conversion_line(test["notes"])
            lines.append(paint(f"        {line}", outcome))
        if outcome == "skipped" and test["reason"]:
            reason = f"skipped: {test['reason']}"
            for part in textwrap.wrap(reason, WIDTH - 8):
                lines.append(paint(f"        {part}", outcome))
    lines += [
        "",
        paint(f"Test summary: {_counts(tests)}", frame, bold=True),
        paint(report["summary"], frame),
        paint(totals_line(tests), frame),
        paint("=" * WIDTH, frame, bold=True),
    ]
    return lines


# Tests that use the real model (the rest use the scripted one).
REAL_TESTS = (
    "test_upload_example_pdf_with_real_model",
    "test_arxiv_link_with_faked_mcp",
)


def _counts(tests):
    order = ("passed", "failed", "skipped")
    counts = {o: sum(t["outcome"] == o for t in tests) for o in order}
    parts = [f"{counts[o]} {o}" for o in order if counts[o]]
    return f"{len(tests)} tests: " + (", ".join(parts) or "none ran")


def use_colour(stream):
    return (
        stream.isatty()
        and not os.environ.get("NO_COLOR")
        and os.environ.get("TERM") != "dumb"
    )


def main(argv=None):
    path, provider, model = (argv or sys.argv[1:])[:3]
    try:
        with open(path, encoding="utf-8") as file:
            report = json.load(file)
    except (OSError, ValueError):
        print("No E2E results to summarise (the tests did not run).")
        return 1
    for line in render(report, provider, model, use_colour(sys.stdout)):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
