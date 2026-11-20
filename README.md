# Paper to Notebook

Upload a research paper PDF and get back a **Google Colab notebook** that
explains the paper and implements its method in runnable Python.

The PDF is sent directly to an LLM (Anthropic Claude or Google Gemini), which
reads the pages as they are — layout, equations, tables and figures — and
writes the notebook.

## How it works

1. `POST /convert` receives the PDF (max 15 MB).
2. The PDF and an instruction prompt go to the provider selected by
   `PROVIDER`. The model replies with JSON matching a fixed schema: title,
   extra pip packages, whether a GPU is needed, and an ordered list of
   markdown/code cells.
3. `notebook_builder` turns that JSON into a valid `.ipynb` with Colab
   metadata, adding a `!pip install` cell and a GPU runtime when needed.
4. The `.ipynb` is returned as a download named after the uploaded file
   (`my_paper.pdf` → `my_paper.ipynb`). A copy is also saved on the
   server in `$NOTEBOOK_OUTPUT_DIR` (see [Output folder](#output-folder)). Open the notebook in Colab with
   **File → Upload notebook**.

A conversion is a paid API call and usually takes a minute or more.

If the model is busy or rate limited (HTTP 408/429/5xx), the request is
retried automatically, up to 5 attempts with growing delays. If it still
fails, `/convert` returns **503** with a `Retry-After` header — try again a
little later, or switch model with `PROVIDER_MODEL`.
Generated code should be reviewed before you rely on its results.

## Setup

Three environment variables are **required** — there are no defaults:

```bash
export PROVIDER=anthropic              # or: gemini
export ANTHROPIC_API_KEY=sk-ant-...    # for gemini: GEMINI_API_KEY=...
export NOTEBOOK_OUTPUT_DIR=~/Downloads # where notebooks are saved
python run.py
```

If any is missing, `run.py` and the server log a warning at startup naming
the variable to export, and `/convert` returns **503** (before any paid API
call is made) until it is set and the server is restarted.

`run.py` creates a virtual environment and installs dependencies on first
run. Then open **http://localhost:8000/**, choose a PDF and click
**Convert to notebook**; the `.ipynb` downloads when it is ready.

The API docs are at `/docs` and a health check at `/health`. From the
command line:

```bash
curl -F "file=@paper.pdf" -OJ http://localhost:8000/convert
```

`POST /convert/stream` takes the same upload but streams progress as
server-sent events (`progress` with the stage — waiting, thinking, writing,
building — elapsed time, characters written and Claude's thinking summary;
then `result` with the notebook, or `error`). The upload page uses it to
show live status.

## LLM providers (Anthropic, Gemini)

[`config.ini`](config.ini) holds the vendor defaults, one section per provider:
`base_url`, `model`, and `api_key_env` (the environment variables to read the
key from, checked in order). **API keys are never stored in a file.**

Each setting comes from the first source that has it:

| Setting  | `get_provider()` kwarg | Environment variable | INI file |
|----------|------------------------|----------------------|----------|
| Provider | `name`                 | `PROVIDER` (required) | — |
| Model    | `model`                | `PROVIDER_MODEL`     | `model`  |
| Base URL | `base_url`             | `PROVIDER_BASE_URL`  | `base_url` |
| API key  | —                      | names in `api_key_env` (required) | never  |

Anthropic tuning, set in `[anthropic]` and overridable per run:

| Option | `config.ini` | Environment override | Notes |
|--------|--------------|----------------------|-------|
| Effort | `effort = medium` | `PROVIDER_EFFORT` | `low`…`max`; higher thinks longer (slower, more tokens) |
| Speed | `speed = fast` | `PROVIDER_SPEED` | `fast` ≈ up to 2.5× faster output at 2× price; falls back to `standard` if busy |
| Max tokens | `max_tokens = 128000` | `PROVIDER_MAX_TOKENS` | shared by thinking and the notebook |

To change defaults without editing `config.ini`, point
`PAPER_TO_NOTEBOOK_CONFIG` at your own `.ini` file; its values override
`config.ini` key by key.

The providers can also be used directly:

```python
from pathlib import Path

from notebook_builder import pdf_to_notebook
from providers import get_provider

result = pdf_to_notebook(Path("paper.pdf").read_bytes(), get_provider())
print(result.filename, len(result.notebook.cells))
```

## Output folder

Every notebook is also saved on the server. `output_dir` under `[app]` in
`config.ini` names the environment variable that holds the folder:

```ini
[app]
output_dir = NOTEBOOK_OUTPUT_DIR
```

The variable is required. `~` and `$VARIABLES` in its value are expanded,
and relative paths are resolved from the project root. Existing files are
never overwritten.

## Logging

The server logs each step of a conversion: which config files were loaded,
the provider, model and base URL (and where each came from), which variable
the API key was found in, the request sent, the reply's stop reason, token
counts and timing, and a summary of the notebook the model returned.

API keys are always masked, e.g. `sk-a…7f2c (34 chars)`. Set
`LOG_LEVEL=DEBUG` to also log the first 500 characters of the model's raw
reply, or `LOG_LEVEL=WARNING` to log only problems.

## Tests

```bash
pytest -v --cov      # fails if coverage drops below 95%
```

The tests use fake LLM clients, so they make no API calls and need no keys.

## Project Structure

```
├── api/
│   ├── main.py             # FastAPI app, /convert endpoint
│   └── static/index.html   # Upload page served at /
├── notebook_builder/
│   ├── prompt.py           # System prompt + JSON schema for the model
│   ├── converter.py        # pdf_to_notebook(): PDF → LLM → notebook
│   └── builder.py          # JSON spec → Colab .ipynb (nbformat)
├── providers/
│   ├── base.py             # LLMProvider interface, ProviderSettings
│   ├── claude.py           # Anthropic SDK
│   ├── gemini.py           # Google Gen AI SDK
│   └── factory.py          # get_provider() from $PROVIDER
├── util/
│   ├── config.py           # config.ini loader, API key lookup
│   ├── files.py            # Save files without overwriting
│   ├── log.py              # Logging setup, secret masking
│   ├── text_utils.py       # Safe filenames
│   └── url_utils.py        # Split API version off base URLs
├── tests/
├── config.ini              # Provider defaults (no keys)
├── output/                 # Saved notebooks (git-ignored)
├── pyproject.toml
├── requirements.txt
└── run.py                  # Entry point
```

## License

MIT — Part of the Modern Software Developer 2.0 bootcamp by [Vizuara AI](https://vizuara.ai).
