#!/usr/bin/env bash
# End-to-end tests: the real dashboard in headless Chrome. Run by hand
# only; ./check.sh and CI leave them out.
#
#   ./e2e.sh            provider, key and model from the environment,
#                       asking for any that is not set
#   ./e2e.sh -k arxiv   extra arguments go to pytest
#
# It checks the environment first: PROVIDER, then that provider's key
# (e.g. ANTHROPIC_API_KEY), then PROVIDER_MODEL. Each one found is
# reported (the key only as "found", never shown); each one missing is
# asked for. No provider or no key given: it exits without running.
#
# The dashboard tests use a scripted model and cost nothing. The two
# conversion tests use the chosen provider and the example paper
# (example/non-linear activation functions.pdf). At the end it prints a
# summary: each test's result and time, and for each real conversion the
# provider, model, tokens, cost and cells, with totals. The key is read without echo, passed only to this test run
# through its environment, and never written to a file.

set -uo pipefail
cd "$(dirname "$0")"

key_var() {
  case "$1" in
    anthropic) echo ANTHROPIC_API_KEY ;;
    gemini) echo GEMINI_API_KEY ;;
    openai) echo OPENAI_API_KEY ;;
    *) return 1 ;;
  esac
}

# A cheaper model than the config default, to keep each run cheap.
cheap_model() {
  case "$1" in
    anthropic) echo claude-sonnet-5-5 ;;
    gemini) echo gemini-3.8-flash ;;
    *) echo "" ;;
  esac
}

# 1. Provider: from $PROVIDER, else ask; none given -> exit.
provider="$(echo "${PROVIDER:-}" | tr '[:upper:]' '[:lower:]' | xargs)"
if [[ -n "$provider" ]]; then
  echo "Provider: $provider (found in \$PROVIDER)"
else
  echo "PROVIDER is not set."
  read -r -p "Provider (anthropic / gemini / openai): " provider
  provider="$(echo "$provider" | tr '[:upper:]' '[:lower:]' | xargs)"
  if [[ -z "$provider" ]]; then
    echo "No provider given; exiting, nothing run." >&2
    exit 2
  fi
fi
if ! var="$(key_var "$provider")"; then
  echo "Unknown provider '$provider' (use anthropic, gemini or openai)." >&2
  exit 2
fi

# 2. Key: from the provider's variable, else ask (hidden); none -> exit.
if [[ -n "${!var:-}" ]]; then
  echo "Key: found in \$$var"
else
  echo "$var is not set."
  read -r -s -p "$var (hidden): " key
  echo
  if [[ -z "$key" ]]; then
    echo "No key given; exiting, nothing run." >&2
    exit 2
  fi
  export "$var=$key"
  unset key
fi

# 3. Model: from $PROVIDER_MODEL, else ask (Enter takes the cheaper one).
if [[ -n "${PROVIDER_MODEL:-}" ]]; then
  echo "Model: $PROVIDER_MODEL (found in \$PROVIDER_MODEL)"
else
  default="$(cheap_model "$provider")"
  read -r -p "Model [${default:-config.toml default}]: " model
  model="${model:-$default}"
  [[ -n "$model" ]] && export PROVIDER_MODEL="$model"
fi

export PROVIDER="$provider"
# Claude: standard speed, since fast mode costs twice as much per token.
if [[ "$provider" == anthropic ]]; then
  export PROVIDER_SPEED="${PROVIDER_SPEED:-standard}"
fi
echo "Running E2E tests with $provider / ${PROVIDER_MODEL:-config.toml model}."

# The tests write their results to a temporary JSON file; the summary
# below is printed from it (green passed, red failed, yellow skipped).
report="$(mktemp)"
trap 'rm -f "$report"' EXIT
TEST_REPORT_JSON="$report" uv run pytest -m e2e -v -p no:logging "$@"
status=$?

echo
uv run python -m tests.e2e.summary "$report" "$provider" \
    "${PROVIDER_MODEL:-config.toml model}"
exit $status
