"""Tests for the notebook depth option (beginner / practitioner / expert).

Depth only changes the system prompt, so it works with every provider.
The LLM is a fake, so no API calls are made.
"""

import json

import pytest
from fastapi.testclient import TestClient
from test_notebook import PDF, FakeProvider
from test_progress import _parse_sse

import api.main as api_main
from notebook_builder import pdf_to_notebook
from notebook_builder.prompt import (
    DEFAULT_DEPTH,
    DEPTHS,
    SYSTEM_PROMPT,
    system_prompt,
)

READER_LINES = {
    "beginner": "new to this field",
    "practitioner": "The reader is a practitioner",
    "expert": "The reader is a researcher in this field",
}


class TestPrompt:
    def test_default_is_practitioner(self):
        assert DEFAULT_DEPTH == "practitioner"
        assert system_prompt() == SYSTEM_PROMPT
        assert READER_LINES["practitioner"] in SYSTEM_PROMPT

    @pytest.mark.parametrize("depth", DEPTHS)
    def test_each_depth_has_its_reader(self, depth):
        prompt = system_prompt(depth)
        assert READER_LINES[depth] in prompt
        for other, line in READER_LINES.items():
            if other != depth:
                assert line not in prompt
        # The shared rules stay in every prompt.
        assert "QUICK_RUN" in prompt
        assert "pip_packages" in prompt

    def test_unknown_depth(self):
        with pytest.raises(ValueError, match="Unknown depth 'guru'"):
            system_prompt("guru")


class TestConverter:
    def test_depth_sets_prompt_and_metadata(self):
        provider = FakeProvider()
        result = pdf_to_notebook(PDF, provider, depth="expert")
        assert provider.calls[0]["system"] == system_prompt("expert")
        assert result.depth == "expert"
        assert result.notebook.metadata["paper_to_notebook"] == {
            "depth": "expert"
        }

    def test_default_depth(self):
        provider = FakeProvider()
        result = pdf_to_notebook(PDF, provider)
        assert provider.calls[0]["system"] == SYSTEM_PROMPT
        assert result.depth == "practitioner"

    def test_unknown_depth_fails_before_the_request(self):
        provider = FakeProvider()
        with pytest.raises(ValueError, match="Unknown depth"):
            pdf_to_notebook(PDF, provider, depth="guru")
        assert provider.calls == []


class TestEndpoints:
    @pytest.fixture
    def client(self):
        provider = FakeProvider()
        overrides = api_main.app.dependency_overrides
        overrides[api_main.get_llm_provider] = lambda: provider
        yield TestClient(api_main.app), provider
        api_main.app.dependency_overrides.clear()

    def _post(self, client, path="/convert/stream", **data):
        return client.post(
            path,
            files={"file": ("paper.pdf", PDF, "application/pdf")},
            data=data,
        )

    def test_stream_uses_depth(self, client):
        test_client, provider = client
        events = _parse_sse(self._post(test_client, depth="beginner").text)
        kind, result = events[-1]
        assert kind == "result"
        assert result["depth"] == "beginner"
        assert READER_LINES["beginner"] in provider.calls[0]["system"]
        notebook = json.loads(result["notebook"])
        assert notebook["metadata"]["paper_to_notebook"]["depth"] == (
            "beginner"
        )

    def test_stream_default_depth(self, client):
        test_client, _ = client
        events = _parse_sse(self._post(test_client).text)
        assert events[-1][1]["depth"] == "practitioner"

    def test_convert_uses_depth(self, client):
        test_client, provider = client
        response = self._post(test_client, path="/convert", depth="expert")
        assert response.status_code == 200
        assert READER_LINES["expert"] in provider.calls[0]["system"]

    @pytest.mark.parametrize("path", ["/convert", "/convert/stream"])
    def test_unknown_depth_is_rejected(self, client, path):
        test_client, provider = client
        response = self._post(test_client, path=path, depth="guru")
        assert response.status_code == 422
        assert provider.calls == []

    def test_page_has_depth_switch(self, client):
        page = client[0].get("/").text
        for depth in DEPTHS:
            assert f'name="depth" value="{depth}"' in page
