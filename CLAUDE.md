# Paper to Notebook

FastAPI server and browser dashboard that turns a research paper PDF (or
an arXiv link) into a Google Colab notebook using an LLM. See README.md
for the architecture; `config.toml` holds provider defaults.

## Git: never commit or push

- Do not run `git commit`, `git push`, `git merge`, `git rebase`,
  `git reset` or any other command that changes git history or the
  remote. The user reviews and commits every change themselves.
- Read-only git commands (`status`, `diff`, `log`, `show`) are fine.
- When work is done, say which files changed and suggest a commit
  message; leave staging and committing to the user.
- Never suggest `--no-verify` or any other way around checks.

## Checks: run before saying work is done

Run `./check.sh` after every change. It runs the same checks as CI
(`.github/workflows/ci.yml`), and the work is not finished until it
passes:

1. `ruff check .`: lint, including the `S` (bandit) security rules
2. `ruff format --check .`: formatting
3. `pip-audit`: known vulnerabilities in dependencies
4. `semgrep --config auto`: security patterns in Python, the dashboard's
   JS/HTML, shell and the CI workflow (needs network)
5. `pytest tests/ --cov`: tests, with coverage of at least 95%

`./check.sh --fix` auto-fixes lint and formatting first.

- Ruff rules are configured in `pyproject.toml` (`[tool.ruff.lint]`).
  Fix the code rather than changing the rules.
- Add `# noqa: <code>` only for a false positive, on that one line,
  with a short reason. Never add a blanket `noqa` or a new ignore.
- For a semgrep finding, fix the code. Only if the behaviour is intended
  (e.g. the run check executing notebook code), add
  `# nosemgrep: <rule>` on the line before, with a reason.
- If pip-audit reports a vulnerability, raise that package's minimum
  version in `pyproject.toml` to the fixed release.
- New code needs tests; keep coverage at 95% or more.

## Tooling

- Use uv: `uv sync --extra dev`, then `uv run <tool>`. Don't use bare
  `pip install`.
- Python 3.11+, line length 79.
- `uv.lock` is git-ignored; don't commit it.

## Code

- `example/` holds LLM-generated notebooks kept exactly as generated;
  don't edit or lint them.
- Never log, print or hard-code API keys; use `util.log.mask_secret`.
- Dashboard UI work (`api/static/`) follows the
  `paper-notebook-ui-design` skill.
- Security-sensitive work (endpoints, running code or subprocesses,
  paths and downloads, API keys, fetched URLs, dashboard code showing
  server or model data, CI, dependencies, triaging findings) follows the
  `security-hardening` skill.
