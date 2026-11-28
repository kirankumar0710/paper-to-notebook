---
name: security-hardening
description: Security review and hardening for Paper-to-Notebook. Use whenever adding or changing an API endpoint, anything that runs code or subprocesses (the run check, the arXiv MCP server), file paths or downloads, handling of API keys or config, uploads or fetched URLs, dashboard code that displays server or model data, CI/CD, or dependencies; when triaging a ruff S, semgrep or pip-audit finding; and for a manual security review or security report (OWASP Top 10, secrets, input validation, rate limiting, CORS, security headers, prompt injection) with severity levels.
---

# Security hardening

The checks themselves live in `./check.sh` (and so in CI). Run it; do not
copy its commands elsewhere. This skill adds what the tools cannot: this
app's threat model, a checklist for new code, how to triage findings,
and a manual security review that ends in a severity-rated report.

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
   - It is **not** a sandbox against malicious code: it runs as the
     user, so it can read their files, use the network and read this
     server's API key from the process table (`ps eww <pid>` on macOS;
     the clean environment only stops accidental leaks). Proper isolation
     needs a container or a separate OS user; do that before any remote
     use.
   - So it is opt-in per run (`[verify] default = false`), offered only
     to localhost clients with a localhost `Host` header (`verify_allowed`
     with `host_is_local`; `[verify] allow_remote = false`), and the
     checkbox says "only for papers you trust".
   - It installs only packages in `[verify] allowed_packages`
     (`split_allowed`); others are skipped and reported, so a paper
     cannot get an arbitrary PyPI package installed here.
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
   - **DNS rebinding** (a page pointing its own domain at 127.0.0.1 is
     same-origin with this server) is stopped by `HostCheckMiddleware`
     (`api/security.py`): only `localhost`, `127.0.0.1`, `::1` and
     `[app] allowed_hosts` are served; anything else gets 400. Never
     allow `"*"` locally.
   - `SecurityHeadersMiddleware` sets a CSP allowing only `'self'`
     (no inline scripts or styles; the docs pages get only
     `frame-ancestors 'none'`), `X-Frame-Options: DENY`,
     `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
     and HSTS over HTTPS. Dashboard code must work under that CSP: no
     inline `<script>`, `style="…"` or `on…=` handlers.
   - At most `[app] max_concurrent_conversions` (2) conversions run at
     once (`CONVERSIONS` slots in `convert.py`); more get 429. A new
     costly endpoint takes a slot after validating its input and
     releases it in `finally`.
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
   - Uploads: `.pdf` name, `%PDF-` header, 15 MB limit (`_read_pdf`
     reads at most limit + 1 bytes).
   - arXiv: the ID is parsed and validated; the PDF is downloaded only
     from arxiv.org hosts, also after redirects, with the same size
     limit; the MCP server starts with a minimal environment.
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
8. **No authentication.** Anyone who can reach the server can spend the
   owner's API key, list and download notebooks and see server paths.
   It is a local tool: never deploy it publicly (or add a host to
   `[app] allowed_hosts`) without authentication in front of it.

## 3. Checklist for new code

Answer each that applies before finishing. Every test of a defence gets
`@pytest.mark.security` (on top of its level marker), so
`uv run pytest -m security` runs them all.


- **New endpoint:** Does it change state, cost money or run code? Then
  add `Depends(require_same_origin)` and a test in
  `tests/test_security.py` that a cross-site request gets 403 before any
  work is done. Costly work takes a `CONVERSIONS` slot. Does its response include anything secret or a server
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

## 5. Manual security review

The tools in section 1 match patterns; they miss design problems (the
wildcard CORS finding only became serious once the run check existed). A
manual review reads the code with an attacker's eye and ends in a report.

**When:** before a release or deploy (e.g. Railway), after a feature that
adds an endpoint, runs code, or changes what the model sees or produces,
and whenever asked for a security review or report.

**How:**

1. Run `./check.sh` and note its results; they feed the report.
2. Map the attack surface: every route in `api/routes/` (method, who can
   call it, what it does), every subprocess, every place untrusted data
   enters (upload, arXiv, model output, config, environment).
3. Review each area below. For each, read the code, then try it where
   possible (curl against a running server, a browser) instead of
   reasoning only. Record what you checked even when it is clean.

| Area | What to check here |
|---|---|
| **OWASP Top 10** | A01 access control: can a request reach files, notebooks or the run check it should not (IDs not paths, localhost gate, `require_same_origin`)? A02 crypto: keys only in env, nothing secret over plain HTTP in a deploy. A03 injection: shell, `exec`, paths, HTML/DOM, the install cell. A04 insecure design: run check scope, defaults. A05 misconfiguration: CORS, headers, debug/reload, `0.0.0.0`, error detail. A06 vulnerable components: pip-audit, pinned CI actions. A07 auth: there is no login, so anything exposed beyond localhost is public; say so. A08 integrity: unpinned actions, packages the model asks for. A09 logging: failures logged, secrets masked. A10 SSRF: fetched URLs limited to arxiv.org. |
| **Hardcoded secrets and API key leaks** | Grep the repo, `config.toml`, tests, docs, `example/` notebooks and git history for key-like strings (`sk-`, `AIza`, `key=`). Follow a key from the environment to every log line, response, error message, notebook metadata, recipe and the sandbox environment; it must appear only masked. |
| **Input validation gaps** | Every form field and query parameter (file, `arxiv`, `verify`, `depth`, `after`, notebook ID): type, allowed values, size limits, and what happens with empty, huge, malformed or unexpected input. Model output is input too: the spec, `pip_packages`, repair fixes. |
| **Rate limiting** | Each conversion costs money on the user's API key and holds a worker thread; the run check uses CPU. Can a client start unlimited conversions in parallel or in a loop? Is there a per-client or global limit, a queue, or a cap on concurrent runs? Rate severity by exposure: local-only is lower; any deploy is higher. |
| **CORS misconfiguration** | No CORS middleware should be present; if any is added, no `"*"` origins and never `"*"` with credentials. Confirm with `curl -H "Origin: https://evil.example"` that no `Access-Control-Allow-*` headers come back, and that a cross-site POST gets 403. |
| **Security headers** | Check responses for `Content-Security-Policy` (scripts and styles from self; the dashboard loads nothing external), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` or CSP `frame-ancestors 'none'` (clickjacking of the Convert button), `Referrer-Policy`, and `Strict-Transport-Security` when deployed over HTTPS. Downloads should keep `Content-Disposition: attachment`. |
| **Prompt injection** | The uploaded or fetched PDF goes to the configured LLM (Anthropic, Gemini or OpenAI) and can contain instructions. Trace what model output is trusted: notebook text shown in the dashboard (must stay `textContent`), `pip_packages` (filtered to plain specs), code that the run check executes (the highest risk: injected instructions can make the model write code that runs on this machine), repair prompts that include error output. Check the system prompt does not rely on the paper behaving, and that a user is warned to review code before running it. |

**Severity levels:**

| Level | Meaning |
|---|---|
| **Critical** | Exploitable now, without unusual conditions: code execution on the server, key disclosure, or arbitrary file access. Fix before anything ships. |
| **High** | Exploitable with a plausible condition (e.g. the user visits a malicious page, or the app is deployed), or causes direct cost or data exposure. Fix before deploy. |
| **Medium** | Weakens a defence or needs several conditions: missing headers, missing rate limits on a local-only server, unpinned supply chain. Fix soon. |
| **Low** | Hardening or hygiene with little direct impact. |
| **Info** | Checked and fine, or accepted by design; state why. |

Rate each finding by impact times likelihood in the app's real
deployment (local by default; say how the rating changes if deployed).

**Report** (in the reply, or in `docs/` when asked for a document):

1. Summary: scope (commit, areas), how it was done, counts by severity.
2. Findings, most severe first. For each: ID, title, severity, area
   (OWASP category where it fits), location (`file:line`), what is wrong,
   how it could be exploited here (concrete scenario), evidence (what you
   ran and saw), and the fix.
3. Checked and fine: each area with no findings and what was verified,
   so the report shows coverage, not just problems.
4. Residual risks accepted by design (e.g. the run check is not a full
   sandbox) and the conditions they depend on.

Do not fix during the review unless asked; report first, then fix by
severity, each fix with a test (section 3) and a re-run of the evidence.
Afterwards, update `docs/security.html` (findings and their status,
remaining risks, settings) so it matches the code.

## 6. Before finishing

- `./check.sh` passes (all five steps).
- For changes to endpoints or the dashboard: start the server and try the
  attack you defended against (e.g. a cross-site POST with
  `-H "Origin: https://evil.example" -H "Sec-Fetch-Site: cross-site"`
  gets 403), and confirm the dashboard still works in a browser.
- Say in the summary what was hardened, what was verified, and any risk
  that remains (e.g. the run check is not a full sandbox).
