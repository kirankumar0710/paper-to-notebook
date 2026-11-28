"""Tests for logging: key masking and what gets logged along the way."""

import logging

import pytest

from notebook_builder import pdf_to_notebook
from providers import ProviderSettings, get_provider
from util.config import ConfigError
from util.log import APP_LOGGER, configure_logging, mask_secret

SECRET = "sk-ant-api03-SUPERSECRETVALUE-7f2c"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("PROVIDER", "PROVIDER_MODEL", "PROVIDER_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    for var in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.security
class TestMaskSecret:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (SECRET, "sk-a…7f2c (34 chars)"),
            ("short-key", "**** (9 chars)"),
            ("", "<not set>"),
            (None, "<not set>"),
        ],
    )
    def test_mask(self, value, expected):
        assert mask_secret(value) == expected


class TestConfigureLogging:
    def test_adds_one_handler_and_sets_level(self):
        logger = logging.getLogger(APP_LOGGER)
        configure_logging("DEBUG")
        configure_logging("DEBUG")
        ours = [h for h in logger.handlers if getattr(h, "_p2n", False)]
        assert len(ours) == 1
        assert logger.level == logging.DEBUG
        configure_logging("INFO")
        assert logger.level == logging.INFO

    def test_level_from_env(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "warning")
        configure_logging()
        assert logging.getLogger(APP_LOGGER).level == logging.WARNING
        configure_logging("INFO")

    def test_short_logger_names(self):
        configure_logging("INFO")
        (handler,) = [
            h
            for h in logging.getLogger(APP_LOGGER).handlers
            if getattr(h, "_p2n", False)
        ]
        record = logging.LogRecord(
            f"{APP_LOGGER}.providers.claude",
            logging.INFO,
            __file__,
            1,
            "hello",
            None,
            None,
        )
        line = handler.format(record)
        assert line.startswith("INFO ")
        assert line.endswith("[providers.claude] hello")


@pytest.mark.security
class TestDescribeKey:
    def test_found(self):
        settings = ProviderSettings(
            name="anthropic",
            model="m",
            api_key=SECRET,
            api_key_source="ANTHROPIC_API_KEY",
        )
        assert settings.describe_key() == (
            "found in ANTHROPIC_API_KEY: sk-a…7f2c (34 chars)"
        )
        assert SECRET not in repr(settings)

    def test_not_found(self):
        settings = ProviderSettings(
            name="gemini", model="m", api_key_env=("GEMINI_API_KEY",)
        )
        assert settings.describe_key() == "not found (GEMINI_API_KEY)"


def _messages(caplog):
    return [r.getMessage() for r in caplog.records]


class TestWhatIsLogged:
    def test_provider_resolution(self, caplog, monkeypatch):
        monkeypatch.setenv("PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
        with caplog.at_level(logging.INFO, logger=APP_LOGGER):
            get_provider()
        messages = _messages(caplog)

        assert any(m.startswith("Loaded config: ") for m in messages)
        assert "Provider: anthropic (from $PROVIDER)" in messages
        assert (
            "Model: claude-opus-5-5 (from config [anthropic] model)"
            in messages
        )
        assert "API key: found in ANTHROPIC_API_KEY: sk-a…7f2c (34 chars)" in (
            messages
        )
        assert any("Anthropic client created" in m for m in messages)
        assert any(m.startswith("Provider ready: ") for m in messages)

    def test_missing_key_is_an_error(self, monkeypatch):
        monkeypatch.setenv("PROVIDER", "anthropic")
        with pytest.raises(ConfigError, match="Export ANTHROPIC_API_KEY."):
            get_provider()

    def test_conversion_steps(self, caplog):
        from test_notebook import PDF, FakeProvider

        with caplog.at_level(logging.INFO, logger=APP_LOGGER):
            pdf_to_notebook(PDF, FakeProvider(), source_name="paper.pdf")
        messages = _messages(caplog)
        assert any(
            m.startswith("Model returned: title='Attention Is All You Need'")
            and "2 cells (1 markdown, 1 code)" in m
            for m in messages
        )
        assert "Dropped unsafe pip package specs: ['bad; rm -rf /']" in (
            messages
        )
        assert "Built notebook 'paper.ipynb' with 5 cells" in messages

    @pytest.mark.security
    def test_key_never_logged_in_full(self, caplog, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
        monkeypatch.setenv("GEMINI_API_KEY", SECRET)
        with caplog.at_level(logging.DEBUG, logger=APP_LOGGER):
            get_provider("anthropic")
            get_provider("gemini")
        assert SECRET not in caplog.text
        assert "sk-a…7f2c" in caplog.text


class TestClaudeProgress:
    def test_logs_progress_while_streaming(self, caplog, monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        import providers.claude as claude_module
        from providers.claude import ClaudeProvider

        monkeypatch.setattr(claude_module, "PROGRESS_LOG_INTERVAL", 0)
        events = [
            SimpleNamespace(
                type="content_block_start",
                content_block=SimpleNamespace(type="thinking"),
            ),
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="thinking_delta"),
            ),
            SimpleNamespace(
                type="content_block_start",
                content_block=SimpleNamespace(type="text"),
            ),
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="text_delta", text='{"ok":'),
            ),
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="text_delta", text=" true}"),
            ),
        ]
        client = MagicMock()
        stream = (
            client.beta.messages.stream.return_value.__enter__.return_value
        )
        stream.__iter__.return_value = iter(events)
        stream.get_final_message.return_value = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text='{"ok": true}')],
        )
        provider = ClaudeProvider(
            ProviderSettings(name="anthropic", model="m"), client=client
        )

        with caplog.at_level(logging.INFO, logger=APP_LOGGER):
            assert provider.generate_json("x", schema={}) == {"ok": True}

        progress = [
            m for m in _messages(caplog) if "Anthropic still working" in m
        ]
        # One line for "waiting", one per event, one for "done".
        assert len(progress) == len(events) + 2
        assert "thinking, 0 chars" in progress[2]
        assert "writing, 12 chars" in progress[-2]
