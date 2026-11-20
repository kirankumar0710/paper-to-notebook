"""Tests for provider config resolution and the LLM providers.

No network calls are made: SDK clients are either faked or only constructed.
"""

import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import anthropic
import httpx2
import pytest
from google.genai import errors as genai_errors
from google.genai.types import FinishReason

from providers import (
    ProviderError,
    ProviderSettings,
    get_provider,
    resolve_settings,
)
from providers.claude import ClaudeProvider
from providers.factory import resolve_provider_name
from providers.gemini import GeminiProvider
from util.config import (
    PROJECT_ROOT,
    ConfigError,
    find_api_key,
    load_config,
    parse_env_var_list,
    resolve_output_dir,
)
from util.url_utils import split_api_version

ENV_VARS = (
    "PROVIDER",
    "PROVIDER_MODEL",
    "PROVIDER_BASE_URL",
    "PAPER_TO_NOTEBOOK_CONFIG",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def override_file(tmp_path):
    path = tmp_path / "override.ini"
    path.write_text("[gemini]\nmodel = gemini-override\n")
    return path


class TestConfigFile:
    def test_vendor_defaults(self):
        config = load_config()
        assert config.get("anthropic", "model") == "claude-opus-5-5"
        assert config.get("gemini", "base_url").endswith("/v1beta")

    def test_no_api_keys_in_vendor_file(self):
        config = load_config()
        for section in config.sections():
            assert "api_key" not in config[section]

    def test_override_file_merges_key_by_key(self, override_file):
        config = load_config(override_file)
        assert config.get("gemini", "model") == "gemini-override"
        # Untouched keys keep the vendor default.
        assert config.get("gemini", "api_key_env") == "GEMINI_API_KEY"

    def test_override_file_from_env(self, override_file, monkeypatch):
        monkeypatch.setenv("PAPER_TO_NOTEBOOK_CONFIG", str(override_file))
        assert load_config().get("gemini", "model") == "gemini-override"

    def test_missing_override_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "missing.ini")


class TestApiKeyEnv:
    def test_parse_list(self):
        assert parse_env_var_list(" A, B ,,C ") == ("A", "B", "C")

    def test_first_set_variable_wins(self, monkeypatch):
        monkeypatch.delenv("TEST_KEY_A", raising=False)
        monkeypatch.setenv("TEST_KEY_B", "b")
        assert find_api_key(("TEST_KEY_A", "TEST_KEY_B")) == "b"
        monkeypatch.setenv("TEST_KEY_A", "a")
        assert find_api_key(("TEST_KEY_A", "TEST_KEY_B")) == "a"

    def test_none_set(self):
        assert find_api_key(("GEMINI_API_KEY",)) is None


class TestSplitApiVersion:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://a.test/v1", ("https://a.test", "v1")),
            ("https://g.test/v1beta/", ("https://g.test", "v1beta")),
            ("https://p.test/api", ("https://p.test/api", None)),
            ("https://p.test", ("https://p.test", None)),
        ],
    )
    def test_split(self, url, expected):
        assert split_api_version(url) == expected


class TestResolveProvider:
    def test_provider_is_required(self):
        with pytest.raises(ConfigError, match="export PROVIDER"):
            resolve_provider_name()

    def test_blank_provider_is_required(self, monkeypatch):
        monkeypatch.setenv("PROVIDER", "   ")
        with pytest.raises(ConfigError, match="export PROVIDER"):
            resolve_provider_name()

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("anthropic", "anthropic"),
            (" Anthropic ", "anthropic"),
            ("claude", "anthropic"),
            ("gemini", "gemini"),
            ("GOOGLE", "gemini"),
        ],
    )
    def test_provider_env_var(self, value, expected, monkeypatch):
        monkeypatch.setenv("PROVIDER", value)
        assert resolve_provider_name() == expected

    def test_argument_overrides_env(self, monkeypatch):
        monkeypatch.setenv("PROVIDER", "gemini")
        assert resolve_provider_name("anthropic") == "anthropic"

    def test_unknown_provider(self, monkeypatch):
        monkeypatch.setenv("PROVIDER", "openai")
        with pytest.raises(ValueError, match="Set PROVIDER"):
            resolve_provider_name()


class TestResolveSettings:
    def test_key_read_from_env(self, monkeypatch):
        monkeypatch.setenv("PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        settings = resolve_settings()
        assert settings.name == "anthropic"
        assert settings.model == "claude-opus-5-5"
        assert settings.base_url == "https://api.anthropic.com/v1"
        assert settings.api_key_env == ("ANTHROPIC_API_KEY",)
        assert settings.api_key == "sk-test"

    def test_gemini_reads_only_gemini_key(self, monkeypatch):
        monkeypatch.setenv("PROVIDER", "gemini")
        monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
        with pytest.raises(ConfigError, match="Export GEMINI_API_KEY."):
            resolve_settings()
        monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
        assert resolve_settings().api_key == "gemini-key"

    def test_env_overrides_ini(self, monkeypatch, override_file):
        monkeypatch.setenv("PROVIDER", "gemini")
        monkeypatch.setenv("PROVIDER_MODEL", "env-model")
        monkeypatch.setenv("PROVIDER_BASE_URL", "https://proxy.test/v1/")
        monkeypatch.setenv("GEMINI_API_KEY", "g-test")
        settings = resolve_settings(config=load_config(override_file))
        assert settings.model == "env-model"
        assert settings.base_url == "https://proxy.test/v1"

    def test_kwargs_override_env(self, monkeypatch):
        monkeypatch.setenv("PROVIDER_MODEL", "env-model")
        monkeypatch.setenv("GEMINI_API_KEY", "g-test")
        settings = resolve_settings("gemini", model="kwarg-model")
        assert settings.model == "kwarg-model"

    def test_empty_model_requires_explicit_choice(self, tmp_path):
        path = tmp_path / "o.ini"
        path.write_text("[anthropic]\nmodel =\n")
        with pytest.raises(ValueError, match="PROVIDER_MODEL"):
            resolve_settings("anthropic", config=load_config(path))

    def test_repr_hides_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
        assert "sk-secret" not in repr(resolve_settings("anthropic"))


class TestGetProvider:
    def test_builds_anthropic(self, monkeypatch):
        monkeypatch.setenv("PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        provider = get_provider()
        assert isinstance(provider, ClaudeProvider)
        # "/v1" is removed because the SDK adds it itself.
        assert str(provider._client.base_url).rstrip("/") == (
            "https://api.anthropic.com"
        )

    def test_builds_gemini(self, monkeypatch):
        monkeypatch.setenv("PROVIDER", "gemini")
        monkeypatch.setenv("GEMINI_API_KEY", "g-test")
        provider = get_provider()
        assert isinstance(provider, GeminiProvider)
        options = provider._client._api_client._http_options
        assert options.api_version == "v1beta"

    @pytest.mark.parametrize("provider", ["gemini", "anthropic"])
    def test_provider_without_key(self, provider, monkeypatch):
        monkeypatch.setenv("PROVIDER", provider)
        with pytest.raises(ConfigError, match="No API key"):
            get_provider()

    def test_gemini_without_key_cannot_be_built(self):
        settings = ProviderSettings(
            name="gemini", model="m", api_key_env=("GEMINI_API_KEY",)
        )
        with pytest.raises(ProviderError, match="Export GEMINI_API_KEY."):
            GeminiProvider(settings)

    def test_claude_without_key_cannot_be_built(self):
        settings = ProviderSettings(
            name="anthropic", model="m", api_key_env=("ANTHROPIC_API_KEY",)
        )
        with pytest.raises(ProviderError, match="Export ANTHROPIC_API_KEY."):
            ClaudeProvider(settings)


SCHEMA = {"type": "object"}
PDF = b"%PDF-1.7 fake"


def _settings(name="anthropic", model="test-model"):
    return ProviderSettings(name=name, model=model)


def _claude_client(text, stop_reason="end_turn"):
    response = SimpleNamespace(
        stop_reason=stop_reason,
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
    )
    client = MagicMock()
    stream = client.beta.messages.stream.return_value.__enter__.return_value
    stream.get_final_message.return_value = response
    return client


class TestClaudeProvider:
    def test_sends_pdf_and_schema(self):
        client = _claude_client('{"ok": true}')
        provider = ClaudeProvider(_settings(), client=client)

        result = provider.generate_json(
            "Convert", schema=SCHEMA, system="Be brief", pdf=PDF
        )

        assert result == {"ok": True}
        request = client.beta.messages.stream.call_args.kwargs
        assert request["model"] == "test-model"
        assert request["system"] == "Be brief"
        assert request["max_tokens"] == 128000
        assert request["output_config"] == {
            "effort": "medium",
            "format": {"type": "json_schema", "schema": SCHEMA},
        }
        assert request["thinking"] == {
            "type": "adaptive",
            "display": "summarized",
        }
        assert "speed" not in request
        assert request["fallbacks"] == "default"
        document, text = request["messages"][0]["content"]
        assert document["type"] == "document"
        assert document["source"]["media_type"] == "application/pdf"
        assert base64.b64decode(document["source"]["data"]) == PDF
        assert text == {"type": "text", "text": "Convert"}

    def test_no_fallbacks_when_disabled(self):
        client = _claude_client("{}")
        provider = ClaudeProvider(_settings(), fallbacks="", client=client)
        provider.generate_json("x", schema=SCHEMA)
        request = client.beta.messages.stream.call_args.kwargs
        assert "fallbacks" not in request
        assert "betas" not in request

    @pytest.mark.parametrize(
        ("stop_reason", "match"),
        [("refusal", "declined"), ("max_tokens", "cut off")],
    )
    def test_bad_stop_reason_raises(self, stop_reason, match):
        client = _claude_client("{}", stop_reason=stop_reason)
        provider = ClaudeProvider(_settings(), client=client)
        with pytest.raises(ProviderError, match=match):
            provider.generate_json("x", schema=SCHEMA)

    def test_invalid_json_raises(self):
        provider = ClaudeProvider(_settings(), client=_claude_client("{"))
        with pytest.raises(ProviderError, match="invalid JSON"):
            provider.generate_json("x", schema=SCHEMA)


def _gemini_client(text, finish_reason=None):
    client = MagicMock()
    client.models.generate_content.return_value = SimpleNamespace(
        text=text,
        candidates=[
            SimpleNamespace(finish_reason=finish_reason or FinishReason.STOP)
        ],
    )
    return client


class TestGeminiProvider:
    def test_sends_pdf_and_schema(self):
        client = _gemini_client('{"ok": true}')
        provider = GeminiProvider(_settings("gemini"), client=client)

        result = provider.generate_json(
            "Convert", schema=SCHEMA, system="Be brief", pdf=PDF
        )

        assert result == {"ok": True}
        call = client.models.generate_content.call_args.kwargs
        assert call["model"] == "test-model"
        pdf_part, prompt = call["contents"]
        assert pdf_part.inline_data.data == PDF
        assert pdf_part.inline_data.mime_type == "application/pdf"
        assert prompt == "Convert"
        assert call["config"].system_instruction == "Be brief"
        assert call["config"].response_mime_type == "application/json"
        assert call["config"].response_json_schema == SCHEMA

    def test_truncated_reply_raises(self):
        client = _gemini_client("{", finish_reason=FinishReason.MAX_TOKENS)
        provider = GeminiProvider(_settings("gemini"), client=client)
        with pytest.raises(ProviderError, match="cut off"):
            provider.generate_json("x", schema=SCHEMA)

    def test_empty_reply_raises(self):
        provider = GeminiProvider(
            _settings("gemini"), client=_gemini_client(None)
        )
        with pytest.raises(ProviderError, match="no content"):
            provider.generate_json("x", schema=SCHEMA)


_REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _status_error(cls, code, message="boom"):
    return cls(
        message, response=httpx2.Response(code, request=_REQUEST), body=None
    )


class TestClaudeErrors:
    @pytest.mark.parametrize(
        ("error", "match"),
        [
            (
                _status_error(anthropic.AuthenticationError, 401),
                "rejected the API key.*ANTHROPIC_API_KEY",
            ),
            (
                _status_error(anthropic.RateLimitError, 429),
                "rate limit",
            ),
            (
                _status_error(anthropic.InternalServerError, 500, "down"),
                "API error 500: down",
            ),
            (
                anthropic.APIConnectionError(request=_REQUEST),
                "Could not connect",
            ),
        ],
    )
    def test_sdk_errors_become_provider_errors(self, error, match):
        client = MagicMock()
        client.beta.messages.stream.side_effect = error
        settings = ProviderSettings(
            name="anthropic",
            model="m",
            api_key_env=("ANTHROPIC_API_KEY",),
        )
        provider = ClaudeProvider(settings, client=client)
        with pytest.raises(ProviderError, match=match) as info:
            provider.generate_json("x", schema=SCHEMA)
        assert info.value.__cause__ is error


class TestGeminiErrors:
    def test_api_error(self):
        client = MagicMock()
        client.models.generate_content.side_effect = genai_errors.ClientError(
            400, {"error": {"code": 400, "message": "bad request"}}
        )
        provider = GeminiProvider(_settings("gemini"), client=client)
        with pytest.raises(ProviderError, match="400: bad request"):
            provider.generate_json("x", schema=SCHEMA)

    def test_invalid_json(self):
        provider = GeminiProvider(
            _settings("gemini"), client=_gemini_client("{not json")
        )
        with pytest.raises(ProviderError, match="invalid JSON"):
            provider.generate_json("x", schema=SCHEMA)


class TestProviderBasics:
    def test_name_and_repr(self):
        provider = GeminiProvider(
            _settings("gemini", "g-1"), client=MagicMock()
        )
        assert provider.name == "gemini"
        assert repr(provider) == "GeminiProvider(model='g-1')"

    def test_missing_key_message_without_env_names(self):
        assert _settings().missing_key_message() == (
            "No API key for 'anthropic'. Export an API key variable."
        )


class TestOutputDir:
    @pytest.fixture(autouse=True)
    def no_env(self, monkeypatch):
        monkeypatch.delenv("NOTEBOOK_OUTPUT_DIR", raising=False)

    def _config(self, tmp_path, value):
        path = tmp_path / "o.ini"
        path.write_text(f"[app]\noutput_dir = {value}\n")
        return load_config(path)

    def test_vendor_config_names_the_env_var(self):
        config = load_config()
        assert config.get("app", "output_dir") == "NOTEBOOK_OUTPUT_DIR"

    def test_variable_is_required(self):
        with pytest.raises(ConfigError, match="export NOTEBOOK_OUTPUT_DIR"):
            resolve_output_dir()

    def test_reads_folder_from_named_variable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NOTEBOOK_OUTPUT_DIR", str(tmp_path / "nb"))
        assert resolve_output_dir() == tmp_path / "nb"

    def test_config_can_name_another_variable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_NOTEBOOKS", str(tmp_path / "mine"))
        monkeypatch.setenv("NOTEBOOK_OUTPUT_DIR", str(tmp_path / "ignored"))
        config = self._config(tmp_path, "MY_NOTEBOOKS")
        assert resolve_output_dir(config) == tmp_path / "mine"

    def test_relative_value_is_from_project_root(self, monkeypatch):
        monkeypatch.setenv("NOTEBOOK_OUTPUT_DIR", "build/notebooks")
        assert resolve_output_dir() == PROJECT_ROOT / "build/notebooks"

    def test_value_expands_home_and_variables(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NOTEBOOK_OUTPUT_DIR", "~/Downloads")
        assert resolve_output_dir() == Path.home() / "Downloads"
        monkeypatch.setenv("BASE", str(tmp_path))
        monkeypatch.setenv("NOTEBOOK_OUTPUT_DIR", "$BASE/papers")
        assert resolve_output_dir() == tmp_path / "papers"

    def test_missing_config_key_is_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NOTEBOOK_OUTPUT_DIR", str(tmp_path))
        with pytest.raises(ConfigError, match="output_dir"):
            resolve_output_dir(self._config(tmp_path, ""))

    def test_path_instead_of_variable_name_is_an_error(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("NOTEBOOK_OUTPUT_DIR", str(tmp_path))
        config = self._config(tmp_path, "~/Downloads")
        with pytest.raises(ConfigError, match="must name an environment"):
            resolve_output_dir(config)
