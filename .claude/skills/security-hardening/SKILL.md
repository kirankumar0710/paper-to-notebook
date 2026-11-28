---
name: security-hardening
description: Security review and hardening for Paper-to-Notebook. Use whenever adding or changing an API endpoint, anything that runs code or subprocesses (the run check, the arXiv MCP server), file paths or downloads, handling of API keys or config, uploads or fetched URLs, dashboard code that displays server or model data, CI/CD, or dependencies; and when triaging a ruff S, semgrep or pip-audit finding.
---

# Security hardening

The checks themselves live in `./check.sh` (and so in CI). Run it; do not
copy its commands elsewhere. This skill adds what the tools cannot: this
app's threat model, a checklist for new code, and how to triage findings.

## 1. Run the checks

Run `./check.sh` after every change. Its security steps:

| Step | Finds |
|---|---|
| `ruff check .` with the `S` (bandit) rules | risky Python: `subprocess`, `exec`, hard-coded secrets, temp files, binding to all interfaces |
| `pip-audit` | dependency versions with known CVEs (the app's packages, including indirect ones; not the run check sandbox's) |
| `semgrep --config auto` | security patterns in Python, dashboard JS/HTML, shell and the CI workflow |

The work is not done until it passes. Tools find patterns; the threat
model below decides whether a finding matters here.

## 2. Threat model

What makes this app risky, and the defence for each. Keep every defence
in place; a change that weakens one needs an explicit reason.

1. **The run check executes LLM-written code on the server**
   (`notebook_builder/verify.py`, `sandbox_runner.py`). The model's code
   can be steered by the paper, so treat it as untrusted.
   - Separate process; minimal environment with no API keys
     (`_sandbox_env`); throwaway working folder; per-cell and total
     timeouts; the process group is killed on timeout.
   - It is not a full sandbox: it can reach the network and the user's
     files. So it is offered only to localhost clients
     (`verify_allowed`; `[verify] allow_remote = false` by default).
     Never enable it by default on a shared or public server.
2. **Other websites can reach a server on localhost through the user's
   browser.** A page can send a form POST to `localhost:8000` without
   asking (no CORS preflight), and the request arrives from 127.0.0.1,
   so it passes the run check's localhost rule.
   - No CORS middleware: the dashboard is same-origin. Do not add CORS
     with `"*"`, and never with `allow_credentials=True`.
   - Every endpoint that changes state, spends money or runs code takes
     `dependencies=[Depends(require_same_origin)]`
     (`api/dependencies.py`): it refuses requests browsers mark as
     cross-site (`Sec-Fetch-Site`, `Origin`). Today: `POST /convert`,
     `POST /convert/stream`.
3. **API keys.** Read only from environment variables, never from files
   or requests; never logged, printed, returned or stored. Log them only
   through `util.log.mask_secret` (`ProviderSettings.__repr__` and
   `describe_key()` already do). Never put a key in notebook metadata,
   the recipe, `/api/config`, error messages or the sandbox environment.
4. **Files and paths.** Downloads are served only by a random ID for
   notebooks this server saved (`/api/notebooks/{id}`), never by a path
   from the request. Page includes must stay inside `api/static/`
   (`is_relative_to`). Saving never overwrites (`save_unique`).
5. **Untrusted input.**
   - Uploads: `.pdf` name, `%PDF-` header, 15 MB limit (`_read_pdf`).
   - arXiv: the ID is parsed and validated; the PDF is downloaded only
     from arxiv.org hosts, with the same size limit; the MCP server
     starts with a minimal environment.
   - Model output: `pip_packages` are filtered to plain package specs
     (`_PACKAGE_SPEC`) and shell-quoted, so the install cell cannot run
     other commands. Everything else the model writes is data until the
     run check runs it (see 1).
6. **The dashboard displays model and server data** (reasoning, cell
   output, errors, file names, recipes). Build DOM with `el()` and set
   text with `textContent`. Never `innerHTML` with server or model data
   (the only `innerHTML` use restores the page's own static hint text).
7. **Supply chain.** CI actions are pinned to full commit SHAs with the
   version in a comment; keep new ones pinned the same way. Dependencies
   come from `pyproject.toml` and are audited by `pip-audit`.

## 3. Checklist for new code

Answer each that applies before finishing:

- **New endpoint:** Does it change state, cost money or run code? Then
  add `Depends(require_same_origin)` and a test in
  `tests/test_security.py` that a cross-site request gets 403 before any
  work is done. Does its response include anything secret or a server
  path that another site should not read?
- **Runs code or a subprocess:** Arguments as a list, not a shell string
  (`shell=True` only for the notebook's own `!` lines in the sandbox).
  No secrets in its environment. A timeout. Kill the whole process group.
- **Takes a path or name from a request or from the model:** Resolve it
  and check it stays inside the intended folder, or look it up by ID.
- **Fetches a URL:** Allow-list the host; limit the size; set a timeout.
- **Handles keys or config:** Masked in logs; absent from responses,
  metadata and sandbox environments; test that it does not leak (see
  `test_saved_notebook_records_its_recipe`).
- **Dashboard code:** `textContent`, not `innerHTML`, for anything from
  the server or the model.
- **CI step:** Actions pinned to a commit SHA; no secrets printed.
- **New dependency:** Needed? Maintained? `pip-audit` clean?

## 4. Triaging a finding

1. **Real issue:** fix the code. Add a test that would have caught it.
2. **Intended behaviour** (e.g. the sandbox runner's `exec()` and
   `shell=True`, which are the point of the run check): add an
   annotation on the line before, with the reason:
   - semgrep: `# nosemgrep: <short-rule-id>` (e.g.
     `# nosemgrep: exec-detected`); it must sit on the line directly
     above the line semgrep reports, which may be an argument line inside
     a call.
   - ruff: `# noqa: <code>` on the same line, with a reason in a comment.
3. **False positive** (e.g. a log line flagged as a credential leak that
   logs a masked key or token counts): annotate as in 2, saying why it
   is not a secret.

Never: blanket `noqa` or `nosemgrep`, new ignores in `pyproject.toml`,
skipping a check, `--no-verify`, or weakening a defence in section 2 to
make a finding go away. If a pip-audit finding has no fixed release,
report it rather than ignoring it.

## 5. Before finishing

- `./check.sh` passes (all five steps).
- For changes to endpoints or the dashboard: start the server and try the
  attack you defended against (e.g. a cross-site POST with
  `-H "Origin: https://evil.example" -H "Sec-Fetch-Site: cross-site"`
  gets 403), and confirm the dashboard still works in a browser.
- Say in the summary what was hardened, what was verified, and any risk
  that remains (e.g. the run check is not a full sandbox).
