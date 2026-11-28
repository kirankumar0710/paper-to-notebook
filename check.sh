#!/usr/bin/env bash
# Run every check the CI pipeline runs. Use this before committing.
#
#   ./check.sh         run all checks, report every failure
#   ./check.sh --fix   auto-fix lint and formatting first, then check
#
# Requires uv: https://docs.astral.sh/uv/

set -uo pipefail
cd "$(dirname "$0")"

fix=false
[[ "${1:-}" == "--fix" ]] && fix=true

failed=()

run() {
    local name="$1"
    shift
    echo
    echo "==> $name"
    if "$@"; then
        echo "--> $name: passed"
    else
        echo "--> $name: FAILED"
        failed+=("$name")
    fi
}

audit() {
    local req
    req="$(mktemp)"
    uv export --extra dev --format requirements-txt \
        --no-emit-project -q -o "$req" \
        && uvx pip-audit -r "$req" --disable-pip --progress-spinner off
    local status=$?
    rm -f "$req"
    return $status
}

echo "==> Installing dependencies"
uv sync --extra dev || exit 1

if $fix; then
    echo
    echo "==> Auto-fixing lint and formatting"
    uv run ruff check . --fix
    uv run ruff format .
fi

run "Lint (ruff check)" uv run ruff check .
run "Format (ruff format --check)" uv run ruff format --check .
run "Dependency vulnerabilities (pip-audit)" audit
# Security patterns in Python, the dashboard's JS/HTML, shell and the CI
# workflow, with the Semgrep registry's recommended rules (--config auto,
# which needs network). example/ holds LLM-generated notebooks kept as
# generated. A finding that is intended gets "# nosemgrep: <rule>" with a
# reason on the line before it.
run "Static analysis (semgrep)" uvx semgrep scan --config auto --error \
    --quiet --exclude example .
run "Tests (pytest + coverage)" uv run pytest tests/ -v --cov

echo
if ((${#failed[@]})); then
    echo "FAILED checks:"
    printf '  - %s\n' "${failed[@]}"
    exit 1
fi
echo "All checks passed."
