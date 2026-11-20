"""Tests for notebook building, PDF conversion and the /convert endpoint.

The LLM is replaced with a fake provider, so no API calls are made.
"""

import asyncio
import io
import json
import os
from pathlib import Path
from typing import Any

import nbformat
import pytest
from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient

import api.main as api_main
from api.main import (
    VERSION,
    convert_pdf_to_notebook,
    get_llm_provider,
    health,
)
from notebook_builder import build_notebook, pdf_to_notebook
from notebook_builder.prompt import NOTEBOOK_SCHEMA, SYSTEM_PROMPT
from providers import LLMProvider, ProviderError, ProviderSettings
from providers.base import Progress

PDF = b"%PDF-1.7 fake paper"

SPEC: dict[str, Any] = {
    "title": "Attention Is All You Need",
    "pip_packages": ["einops", "torch>=2.0", "einops", "bad; rm -rf /"],
    "requires_gpu": False,
    "cells": [
        {"cell_type": "markdown", "source": "## Overview\n\n$a^2$"},
        {"cell_type": "code", "source": "\nimport torch\nprint(1)\n"},
    ],
}


class FakeProvider(LLMProvider):
    def __init__(self, reply=None, error=None):
        super().__init__(ProviderSettings(name="fake", model="fake-model"))
        self.reply = SPEC if reply is None else reply
        self.error = error
        self.calls = []

    def generate_json(
        self, prompt, *, schema, system=None, pdf=None, on_progress=None
    ):
        self.calls.append(
            {"prompt": prompt, "schema": schema, "system": system, "pdf": pdf}
        )
        if on_progress is not None:
            on_progress(Progress("thinking", "Thinking…", thinking="Hmm"))
            on_progress(Progress("writing", "Writing…", output_chars=42))
        if self.error:
            raise self.error
        return self.reply


class TestBuildNotebook:
    def test_cells(self):
        notebook = build_notebook(SPEC)
        types = [cell.cell_type for cell in notebook.cells]
        assert types == ["markdown", "code", "markdown", "code"]
        assert notebook.cells[0].source.startswith(
            "# Attention Is All You Need"
        )
        assert notebook.cells[3].source == "import torch\nprint(1)"
        nbformat.validate(notebook)

    def test_pip_cell_dedupes_and_drops_unsafe_specs(self):
        install = build_notebook(SPEC).cells[1].source
        assert install == "!pip install -q einops 'torch>=2.0'"

    def test_no_pip_cell_without_packages(self):
        notebook = build_notebook({**SPEC, "pip_packages": []})
        assert notebook.cells[1].cell_type == "markdown"

    def test_colab_metadata(self):
        notebook = build_notebook(SPEC, filename="paper.ipynb")
        assert notebook.metadata["colab"]["name"] == "paper.ipynb"
        assert notebook.metadata["kernelspec"]["name"] == "python3"
        assert "accelerator" not in notebook.metadata

    def test_gpu_metadata(self):
        notebook = build_notebook({**SPEC, "requires_gpu": True})
        assert notebook.metadata["accelerator"] == "GPU"
        assert notebook.metadata["colab"]["gpuType"] == "T4"

    @pytest.mark.parametrize(
        "bad_spec",
        [
            {**SPEC, "title": None},
            {**SPEC, "cells": []},
            {**SPEC, "cells": [{"cell_type": "raw", "source": "x"}]},
            {**SPEC, "cells": [{"cell_type": "code"}]},
        ],
    )
    def test_invalid_spec(self, bad_spec):
        with pytest.raises(ValueError):
            build_notebook(bad_spec)


class TestPdfToNotebook:
    def test_sends_pdf_prompt_and_schema(self):
        provider = FakeProvider()
        result = pdf_to_notebook(PDF, provider)

        (call,) = provider.calls
        assert call["pdf"] == PDF
        assert call["schema"] is NOTEBOOK_SCHEMA
        assert call["system"] == SYSTEM_PROMPT
        assert result.title == "Attention Is All You Need"
        assert result.filename == "Attention Is All You Need.ipynb"

    @pytest.mark.parametrize(
        ("source_name", "expected"),
        [
            ("my_paper.pdf", "my_paper.ipynb"),
            ("Attention v2.PDF", "Attention v2.ipynb"),
            ("uploads/dir/paper-1.pdf", "paper-1.ipynb"),
            # Nothing usable in the name: fall back to the paper's title.
            ("注意力.pdf", "Attention Is All You Need.ipynb"),
        ],
    )
    def test_named_after_uploaded_file(self, source_name, expected):
        result = pdf_to_notebook(PDF, FakeProvider(), source_name=source_name)
        assert result.filename == expected
        assert result.notebook.metadata["colab"]["name"] == expected

    def test_default_name_when_nothing_usable(self):
        provider = FakeProvider(reply={**SPEC, "title": "注意力"})
        result = pdf_to_notebook(PDF, provider, source_name="ü.pdf")
        assert result.filename == "notebook.ipynb"


def _upload(data=PDF, filename="paper.pdf"):
    return UploadFile(io.BytesIO(data), filename=filename)


def _convert(provider, **upload_kwargs):
    # conftest.py points NOTEBOOK_OUTPUT_DIR at a temp folder.
    output_dir = Path(os.environ["NOTEBOOK_OUTPUT_DIR"])
    return asyncio.run(
        convert_pdf_to_notebook(_upload(**upload_kwargs), provider, output_dir)
    )


class TestConvertEndpoint:
    def test_returns_colab_notebook(self):
        response = _convert(FakeProvider())
        assert response.status_code == 200
        assert response.media_type == "application/x-ipynb+json"
        # Named after the uploaded file, not the paper's title.
        assert response.headers["content-disposition"] == (
            'attachment; filename="paper.ipynb"'
        )
        notebook = json.loads(response.body)
        assert notebook["metadata"]["colab"]["name"].endswith(".ipynb")

    @pytest.mark.parametrize(
        ("kwargs", "detail"),
        [
            ({"filename": "paper.txt"}, "Only PDF"),
            ({"data": b"not a pdf"}, "not a valid PDF"),
        ],
    )
    def test_rejects_bad_uploads(self, kwargs, detail):
        with pytest.raises(HTTPException) as info:
            _convert(FakeProvider(), **kwargs)
        assert info.value.status_code == 400
        assert detail in info.value.detail

    def test_provider_error_is_502(self):
        provider = FakeProvider(error=ProviderError("rate limit"))
        with pytest.raises(HTTPException) as info:
            _convert(provider)
        assert info.value.status_code == 502
        assert "rate limit" in info.value.detail

    def test_failure_is_logged_with_cause(self, caplog):
        cause = RuntimeError("HTTP 400: invalid schema")
        error = ProviderError("Anthropic API error 400")
        error.__cause__ = cause
        with (
            caplog.at_level("ERROR", logger="uvicorn.error"),
            pytest.raises(HTTPException),
        ):
            _convert(FakeProvider(error=error))
        message = caplog.records[-1].getMessage()
        assert "provider=fake, model=fake-model" in message
        assert "Anthropic API error 400" in message
        assert "cause: RuntimeError: HTTP 400: invalid schema" in message

    def test_unusable_reply_is_502(self):
        with pytest.raises(HTTPException) as info:
            _convert(FakeProvider(reply={"title": "x", "cells": []}))
        assert info.value.status_code == 502
        assert "unusable notebook" in info.value.detail


class TestApiExtras:
    def test_health(self):
        assert asyncio.run(health()) == {
            "status": "running",
            "version": VERSION,
        }

    def test_rejects_large_upload(self, monkeypatch):
        import api.routes.convert as convert_routes

        monkeypatch.setattr(convert_routes, "MAX_UPLOAD_BYTES", 10)
        with pytest.raises(HTTPException) as info:
            _convert(FakeProvider(), data=PDF + b"x" * 20)
        assert info.value.status_code == 400
        assert "too large" in info.value.detail

    def test_unconfigured_provider_is_503(self, monkeypatch):
        monkeypatch.setenv("PROVIDER", "gemini")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        get_llm_provider.cache_clear()
        try:
            with pytest.raises(HTTPException) as info:
                get_llm_provider()
        finally:
            get_llm_provider.cache_clear()
        assert info.value.status_code == 503
        assert "GEMINI_API_KEY" in info.value.detail

    def test_provider_is_created_once(self, monkeypatch):
        monkeypatch.setenv("PROVIDER", "gemini")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        get_llm_provider.cache_clear()
        try:
            assert get_llm_provider() is get_llm_provider()
        finally:
            get_llm_provider.cache_clear()


class TestHttp:
    """Exercise /convert over HTTP, as the Swagger UI and curl do."""

    @pytest.fixture
    def client(self):
        api_main.app.dependency_overrides[get_llm_provider] = FakeProvider
        try:
            yield TestClient(api_main.app)
        finally:
            api_main.app.dependency_overrides.clear()

    def test_multipart_upload(self, client):
        response = client.post(
            "/convert",
            files={"file": ("My Paper 2024.pdf", PDF, "application/pdf")},
        )
        assert response.status_code == 200
        assert response.headers["content-disposition"] == (
            'attachment; filename="My Paper 2024.ipynb"'
        )
        assert json.loads(response.content)["cells"]

    def test_upload_page(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        page = response.text
        assert '<input id="file" name="file" type="file"' in page
        assert 'id="thinking"' in page
        assert '<script type="module" src="/static/app.js">' in page
        script = client.get("/static/app.js").text
        assert 'fetch("/convert/stream"' in script

    def test_missing_file_is_422(self, client):
        assert client.post("/convert").status_code == 422

    def test_docs_show_file_picker(self):
        # Swagger UI renders a file input only for "format: binary".
        schema = api_main.app.openapi()["components"]["schemas"]
        file_field = schema["Body_convert_pdf_to_notebook_convert_post"][
            "properties"
        ]["file"]
        assert file_field["type"] == "string"
        assert file_field["format"] == "binary"


class TestSavedCopy:
    def test_notebook_saved_on_server(self, notebook_output_dir):
        response = _convert(FakeProvider(), filename="2403.03507v2.pdf")
        saved = notebook_output_dir / "2403.03507v2.ipynb"
        assert response.headers["x-notebook-path"] == str(saved)
        assert response.headers["content-disposition"] == (
            'attachment; filename="2403.03507v2.ipynb"'
        )
        assert json.loads(saved.read_text()) == json.loads(response.body)

    def test_second_conversion_gets_new_name(self, notebook_output_dir):
        _convert(FakeProvider())
        response = _convert(FakeProvider())
        assert response.headers["x-notebook-path"] == str(
            notebook_output_dir / "paper (1).ipynb"
        )

    def test_save_failure_does_not_fail_request(
        self, monkeypatch, tmp_path, caplog
    ):
        blocker = tmp_path / "not-a-folder"
        blocker.write_text("x")
        monkeypatch.setenv("NOTEBOOK_OUTPUT_DIR", str(blocker / "sub"))
        response = _convert(FakeProvider())
        assert response.status_code == 200
        assert "x-notebook-path" not in response.headers
        assert "Could not save a copy" in caplog.text

    def test_page_has_download_button(self):
        page = TestClient(api_main.app).get("/").text
        assert 'id="download-link"' in page
        script = api_main.UPLOAD_PAGE.parent / "panels/download/download.js"
        assert "result.saved_path" in script.read_text()

    def test_page_includes_every_panel(self):
        page = TestClient(api_main.app).get("/").text
        assert "<!-- include:" not in page
        for panel in ("config", "upload", "output", "metrics", "download"):
            assert f'id="{panel}-panel"' in page

    def test_panel_assets_are_served(self):
        client = TestClient(api_main.app)
        for path in (
            "/static/app.js",
            "/static/shared.js",
            "/static/base.css",
            "/static/panels/output/output.js",
            "/static/panels/metrics/metrics.css",
        ):
            assert client.get(path).status_code == 200, path

    def test_include_outside_static_dir_is_refused(self, tmp_path):
        from api.routes.dashboard import render_page

        static = tmp_path / "static"
        static.mkdir()
        (tmp_path / "secret.txt").write_text("secret")
        page = static / "index.html"
        page.write_text("<!-- include: ../secret.txt -->")
        with pytest.raises(FileNotFoundError):
            render_page(page, static)


class TestRequiredConfiguration:
    @pytest.fixture(autouse=True)
    def configured(self, monkeypatch):
        monkeypatch.setenv("PROVIDER", "gemini")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key-000000000000")

    @pytest.fixture
    def bad_output_config(self, tmp_path, monkeypatch):
        """Point [app] output_dir at a path instead of a variable name."""
        path = tmp_path / "bad.ini"
        path.write_text("[app]\noutput_dir = ~/notebooks\n")
        monkeypatch.setenv("PAPER_TO_NOTEBOOK_CONFIG", str(path))

    def test_unset_output_dir_defaults_to_downloads(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("NOTEBOOK_OUTPUT_DIR")
        monkeypatch.setenv("HOME", str(tmp_path))
        assert api_main.get_output_dir() == tmp_path / "Downloads"
        assert api_main.check_configuration() == []

    def test_invalid_output_dir_is_503(self, bad_output_config):
        with pytest.raises(HTTPException) as info:
            api_main.get_output_dir()
        assert info.value.status_code == 503
        assert "must name an environment variable" in info.value.detail

    def test_output_dir_checked_before_llm_call(self, bad_output_config):
        provider = FakeProvider()
        api_main.app.dependency_overrides[get_llm_provider] = lambda: provider
        try:
            response = TestClient(api_main.app).post(
                "/convert",
                files={"file": ("paper.pdf", PDF, "application/pdf")},
            )
        finally:
            api_main.app.dependency_overrides.clear()
        assert response.status_code == 503
        assert "output_dir" in response.json()["detail"]
        assert provider.calls == []  # no paid request was made

    def test_check_configuration_ok(self):
        assert api_main.check_configuration() == []

    def test_check_configuration_lists_every_problem(
        self, monkeypatch, bad_output_config
    ):
        monkeypatch.delenv("PROVIDER")
        problems = api_main.check_configuration()
        assert len(problems) == 2
        assert "export PROVIDER" in problems[0]
        assert "output_dir" in problems[1]

    def test_startup_warns_about_problems(self, monkeypatch, caplog):
        monkeypatch.delenv("PROVIDER")
        with TestClient(api_main.app):  # runs the startup (lifespan) hook
            pass
        warnings = [
            r.getMessage() for r in caplog.records if r.levelname == "WARNING"
        ]
        assert any("export PROVIDER" in w for w in warnings)
        assert any("/convert will return 503" in w for w in warnings)

    def test_startup_quiet_when_configured(self, caplog):
        with TestClient(api_main.app):
            pass
        assert not [r for r in caplog.records if r.levelname == "WARNING"]
