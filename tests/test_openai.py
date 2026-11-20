"""Tests for the OpenAI provider. The SDK client is mocked: no API calls."""

import base64
from types import SimpleNamespace as Namespace
from unittest.mock import MagicMock

import httpx
import openai
import pytest

from notebook_builder.prompt import NOTEBOOK_SCHEMA
from providers import ProviderError, ProviderSettings, get_provider
from providers.factory import resolve_provider_name
from providers.openai_provider import OpenAIProvider

PDF = b"%PDF-1.7 fake"


def _response(text='{"ok": true}', status="completed", **kwargs):
    defaults = {
        "model": "gpt-test-2026",
        "output": [],
        "incomplete_details": None,
        "usage": Namespace(
            input_tokens=900, output_tokens=300, total_tokens=1200
        ),
    }
    defaults.update(kwargs)
    return Namespace(output_text=text, status=status, **defaults)


def _provider(response=None, error=None, **options):
    client = MagicMock()
    if error is not None:
        client.responses.create.side_effect = error
    else:
        client.responses.create.return_value = response or _response()
    settings = ProviderSettings(
        name="openai", model="gpt-test", options=options
    )
    return OpenAIProvider(settings, client=client), client


def _api_error(cls, status, code=None):
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    body = {"error": {"message": "nope", "code": code}}
    return cls(
        "nope",
        response=httpx.Response(status, request=request, json=body),
        body=body["error"],
    )


class TestRequest:
    def test_sends_pdf_schema_and_instructions(self):
        provider, client = _provider()
        assert provider.generate_json(
            "Make a notebook",
            schema=NOTEBOOK_SCHEMA,
            system="Be good",
            pdf=PDF,
        ) == {"ok": True}
        request = client.responses.create.call_args.kwargs
        assert request["model"] == "gpt-test"
        assert request["instructions"] == "Be good"
        assert request["store"] is False
        file_part, text_part = request["input"][0]["content"]
        assert file_part["type"] == "input_file"
        assert file_part["file_data"] == (
            "data:application/pdf;base64," + base64.b64encode(PDF).decode()
        )
        assert text_part == {"type": "input_text", "text": "Make a notebook"}
        assert request["text"]["format"] == {
            "type": "json_schema",
            "name": "colab_notebook",
            "schema": NOTEBOOK_SCHEMA,
            "strict": True,
        }

    def test_tuning_not_sent_unless_set(self):
        provider, client = _provider()
        provider.generate_json("x", schema={})
        request = client.responses.create.call_args.kwargs
        assert "reasoning" not in request
        assert "max_output_tokens" not in request
        assert provider.max_output_tokens is None

    def test_effort_and_budget(self):
        provider, client = _provider(effort="HIGH", max_tokens="64000")
        provider.generate_json("x", schema={})
        request = client.responses.create.call_args.kwargs
        assert request["reasoning"] == {"effort": "high"}
        assert request["max_output_tokens"] == 64000
        assert provider.max_output_tokens == 64000

    @pytest.mark.parametrize(
        ("options", "match"),
        [
            ({"effort": "huge"}, "Invalid effort"),
            ({"max_tokens": "0"}, "max_tokens"),
        ],
    )
    def test_invalid_options(self, options, match):
        with pytest.raises(ProviderError, match=match):
            _provider(**options)


class TestReply:
    def test_progress_reports_usage_and_model(self):
        provider, _ = _provider()
        updates = []
        provider.generate_json("x", schema={}, on_progress=updates.append)
        assert [u.stage for u in updates] == ["waiting", "done"]
        done = updates[-1]
        assert done.usage == {
            "input_tokens": 900,
            "output_tokens": 300,
            "total_tokens": 1200,
        }
        assert done.served["model"] == "gpt-test-2026"
        assert done.served["speed"] is None

    def test_cut_off_reply(self):
        response = _response(
            status="incomplete",
            incomplete_details=Namespace(reason="max_output_tokens"),
        )
        provider, _ = _provider(response, max_tokens="1000")
        with pytest.raises(ProviderError, match="PROVIDER_MAX_TOKENS"):
            provider.generate_json("x", schema={})

    def test_refusal(self):
        refusal = Namespace(type="refusal", refusal="Can't help with that.")
        response = _response(
            text="", output=[Namespace(type="message", content=[refusal])]
        )
        provider, _ = _provider(response)
        with pytest.raises(ProviderError, match="declined.*Can't help"):
            provider.generate_json("x", schema={})

    def test_invalid_json(self):
        provider, _ = _provider(_response(text="not json"))
        with pytest.raises(ProviderError, match="invalid JSON"):
            provider.generate_json("x", schema={})


class TestErrors:
    def test_rate_limit_is_retryable(self):
        provider, _ = _provider(error=_api_error(openai.RateLimitError, 429))
        with pytest.raises(ProviderError) as info:
            provider.generate_json("x", schema={})
        assert info.value.retryable is True

    def test_no_quota_is_not_retryable(self):
        error = _api_error(
            openai.RateLimitError, 429, code="insufficient_quota"
        )
        provider, _ = _provider(error=error)
        with pytest.raises(ProviderError, match="no quota") as info:
            provider.generate_json("x", schema={})
        assert info.value.retryable is False

    def test_bad_key(self):
        error = _api_error(openai.AuthenticationError, 401)
        provider, _ = _provider(error=error)
        with pytest.raises(ProviderError, match="rejected the API key"):
            provider.generate_json("x", schema={})


class TestFactory:
    @pytest.fixture(autouse=True)
    def env(self, monkeypatch):
        for var in (
            "PROVIDER_MODEL",
            "PROVIDER_EFFORT",
            "PROVIDER_MAX_TOKENS",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("PROVIDER", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-0000000000000000")

    @pytest.mark.parametrize("alias", ["openai", "OpenAI", "gpt", "chatgpt"])
    def test_aliases(self, alias):
        assert resolve_provider_name(alias) == "openai"

    def test_default_model(self):
        assert get_provider().model == "gpt-5.5"

    def test_model_is_required_if_config_has_none(self, tmp_path, monkeypatch):
        override = tmp_path / "o.ini"
        override.write_text("[openai]\nmodel =\n")
        monkeypatch.setenv("PAPER_TO_NOTEBOOK_CONFIG", str(override))
        with pytest.raises(ValueError, match="PROVIDER_MODEL"):
            get_provider()

    def test_builds_provider(self, monkeypatch):
        monkeypatch.setenv("PROVIDER_MODEL", "gpt-test")
        monkeypatch.setenv("PROVIDER_EFFORT", "low")
        provider = get_provider()
        assert isinstance(provider, OpenAIProvider)
        assert provider.model == "gpt-test"
        assert provider.effort == "low"

    def test_dashboard_shows_provider_without_model(
        self, tmp_path, monkeypatch
    ):
        from api.routes.config import describe_configuration

        override = tmp_path / "o.ini"
        override.write_text("[openai]\nmodel =\n")
        monkeypatch.setenv("PAPER_TO_NOTEBOOK_CONFIG", str(override))
        monkeypatch.delenv("OPENAI_API_KEY")
        info = describe_configuration()
        assert info["provider"] == "openai"
        assert info["model"] == ""
        assert info["api_key"]["env_vars"] == ["OPENAI_API_KEY"]
        assert any("PROVIDER_MODEL" in p for p in info["problems"])


class TestRateLimitDetails:
    def test_reason_is_shown(self):
        error = _api_error(openai.RateLimitError, 429)
        provider, _ = _provider(error=error)
        with pytest.raises(ProviderError, match="rate limit reached: nope"):
            provider.generate_json("x", schema={})

    def test_request_too_large_is_not_retried(self):
        request = httpx.Request("POST", "https://api.openai.com/v1/responses")
        message = (
            "Request too large for gpt-5.5 on tokens per min (TPM): "
            "Limit 30000, Requested 52000."
        )
        body = {"message": message, "code": "rate_limit_exceeded"}
        error = openai.RateLimitError(
            message,
            response=httpx.Response(429, request=request, json=body),
            body=body,
        )
        provider, _ = _provider(error=error)
        with pytest.raises(ProviderError, match="per-minute limit") as info:
            provider.generate_json("x", schema={})
        assert info.value.retryable is False
        assert "Requested 52000" in str(info.value)
