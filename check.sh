#!/usr/bin/env bash
# Run every check the CI pipeline runs. Use this before committing.
#
#   ./check.sh         run all checks, report every failure
#   ./check.sh --fix   auto-fix lint and formatting first, then check
#
# Ends with a summary: each check, what kind of check it is, its result
# and counts (files, rules, packages, tests by type, coverage). In GitHub
# Actions the summary is also written to the run's summary page. The
# summary is information only; pass/fail and the exit code come from the
# checks themselves.
#
# Requires uv: https://docs.astral.sh/uv/

set -uo pipefail
cd "$(dirname "$0")"

fix=false
[[ "${1:-}" == "--fix" ]] && fix=true

logs="$(mktemp -d)"
trap 'rm -rf "$logs"' EXIT

failed=()
rows=() # "name|kind|result|detail|seconds"

# run NAME KIND DETAIL_FUNCTION COMMAND...: show the output and keep a
# copy, then add a summary row from DETAIL_FUNCTION (which reads the log).
run() {
    local name="$1" kind="$2" detail_fn="$3"
    shift 3
    local log="$logs/${#rows[@]}.log" start=$SECONDS result
    echo
    echo "==> $name"
    "$@" 2>&1 | tee "$log"
    if ((PIPESTATUS[0] == 0)); then
        result=passed
        echo "--> $name: passed"
    else
        result=FAILED
        echo "--> $name: FAILED"
        failed+=("$name")
    fi
    rows+=("$name|$kind|$result|$("$detail_fn" "$log")|$((SECONDS - start))")
}

# ---- detail lines for the summary, read from each step's output ----------

lint_detail() {
    local files rules
    files="$(uv run ruff check . --show-files 2>/dev/null | grep -c .)"
    rules="$(sed -n '/^select = \[/,/^\]/p' pyproject.toml \
        | grep -o '"[A-Z]*"' | tr -d '"' | xargs)"
    if grep -q "All checks passed" "$1"; then
        echo "$files files, rules $rules: no issues"
    else
        echo "$files files, rules $rules: $(grep -Eo 'Found [0-9]+ errors?' "$1" | tail -1)"
    fi
}

format_detail() {
    grep -E "files? (already formatted|would be reformatted)" "$1" | tail -1
}

audit_detail() {
    local packages
    packages="$(cat "$logs/packages" 2>/dev/null || echo "?")"
    if grep -q "No known vulnerabilities found" "$1"; then
        echo "$packages packages: no known vulnerabilities"
    else
        echo "$packages packages: $(grep -Eo 'Found [0-9]+ known vulnerabilit[a-z]+ in [0-9]+ packages?' "$1" | tail -1)"
    fi
}

semgrep_detail() {
    # "Ran 495 rules on 85 files: 0 findings."
    grep -Eo 'Ran [0-9]+ rules on [0-9]+ files: [0-9]+ findings?' "$1" \
        | tail -1 | sed 's/^Ran //'
}

tests_detail() {
    local totals types coverage
    totals="$(grep -E '^=+ .*(passed|failed|error).* in [0-9.]+s' "$1" \
        | tail -1 | sed -E 's/^=+ //; s/ in [0-9.]+s.*//')"
    types="$(grep '^Tests by type: ' "$1" | tail -1 \
        | sed 's/^Tests by type: //')"
    coverage="$(grep -Eo 'Total coverage: [0-9.]+%' "$1" | tail -1 \
        | sed 's/Total //')"
    totals="${totals/deselected/deselected (E2E: run ./e2e.sh)}"
    echo "$totals; $types; ${coverage:-coverage not reported} (min 95%)"
}

audit() {
    local req
    req="$(mktemp)"
    uv export --extra dev --format requirements-txt \
        --no-emit-project -q -o "$req" \
        && grep -cE '^[A-Za-z0-9]' "$req" >"$logs/packages" \
        && uvx pip-audit -r "$req" --disable-pip --progress-spinner off
    local status=$?
    rm -f "$req"
    return $status
}

# ---- the checks -----------------------------------------------------------

echo "==> Installing dependencies"
uv sync --extra dev || exit 1

if $fix; then
    echo
    echo "==> Auto-fixing lint and formatting"
    uv run ruff check . --fix
    uv run ruff format .
fi

run "Lint (ruff check)" "Code quality + bandit security rules" \
    lint_detail uv run ruff check .
run "Format (ruff format --check)" "Code style" \
    format_detail uv run ruff format --check .
run "Dependency vulnerabilities (pip-audit)" "Known CVEs in dependencies" \
    audit_detail audit
# Security patterns in Python, the dashboard's JS/HTML, shell and the CI
# workflow, with the Semgrep registry's recommended rules (--config auto,
# which needs network). example/ holds LLM-generated notebooks kept as
# generated. A finding that is intended gets "# nosemgrep: <rule>" with a
# reason on the line before it.
run "Static analysis (semgrep)" "Security patterns (SAST)" \
    semgrep_detail uvx semgrep scan --config auto --error --exclude example .
# E2E browser tests are left out (pyproject.toml); run them with ./e2e.sh.
run "Tests (pytest + coverage)" "Unit, integration, security tests" \
    tests_detail uv run pytest tests/ -v --cov

# ---- summary ---------------------------------------------------------------

# Colour only where it shows: a terminal (or GitHub Actions' log), unless
# NO_COLOR is set (https://no-color.org) or the terminal is "dumb".
green="" red="" bold="" reset=""
if [[ -z "${NO_COLOR:-}" && "${TERM:-}" != dumb ]] \
    && [[ -t 1 || -n "${GITHUB_ACTIONS:-}" ]]; then
    green=$'\e[32m' red=$'\e[31m' bold=$'\e[1m' reset=$'\e[0m'
fi
# The frame is green when everything passed, red otherwise.
frame="$green"
((${#failed[@]})) && frame="$red"

echo
echo "${frame}${bold}==================================== Summary ====================================${reset}"
for row in "${rows[@]}"; do
    IFS='|' read -r name kind result detail seconds <<<"$row"
    colour="$green"
    [[ "$result" == passed ]] || colour="$red"
    printf '%s%s%-7s %-40s %4ss%s\n' \
        "$colour" "$bold" "$result" "$name" "$seconds" "$reset"
    {
        printf '        %s\n' "$kind"
        printf '        %s\n' "$detail" | fold -s -w 72 \
            | sed '2,$s/^/        /'
    } | sed "s/^/$colour/; s/\$/$reset/"
done
echo "${frame}${bold}=================================================================================${reset}"

# In GitHub Actions, also show the summary on the run's page.
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    {
        echo "### Checks"
        echo
        echo "| Result | Check | Type | Details | Time |"
        echo "|---|---|---|---|---|"
        for row in "${rows[@]}"; do
            IFS='|' read -r name kind result detail seconds <<<"$row"
            icon="✅"
            [[ "$result" == passed ]] || icon="❌"
            echo "| $icon $result | $name | $kind | $detail | ${seconds}s |"
        done
    } >>"$GITHUB_STEP_SUMMARY"
fi

echo
if ((${#failed[@]})); then
    echo "${red}${bold}FAILED checks:${reset}"
    printf "${red}  - %s${reset}\n" "${failed[@]}"
    exit 1
fi
echo "${green}${bold}All checks passed.${reset}"
