"""Unit tests for AlertSourceBase: configurable message templates (preset
selection, custom override, bad-template fallback) and the shared datetime/
translate helpers every source builds on.
"""

import configparser
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from modules.command_manager import CommandManager
from modules.i18n import Translator
from modules.service_plugins.alert_modules.base import AlertSourceBase
from modules.service_plugins.alerts_service import AlertsService

PRESETS = {
    "compact": "{prefix}: {title}{date_part}",
    "minimal": "{title}",
}


class _DummySource(AlertSourceBase):
    name = "dummy"

    def is_enabled(self) -> bool:
        return True

    async def check(self) -> None:
        pass

    def render(self, **context) -> str:
        return self.render_template(PRESETS, "compact", **context)


def _bot(**config_overrides):
    bot = MagicMock()
    bot.logger = Mock()
    bot.translator = Translator("en")
    bot.command_manager.split_text_into_utf8_chunks = CommandManager.split_text_into_utf8_chunks
    bot.command_manager.send_channel_messages_chunked = AsyncMock(return_value=True)
    config = configparser.ConfigParser()
    config.add_section("Alerts_Service")
    config.set("Alerts_Service", "enabled", "true")
    config.set("Alerts_Service", "channel", "general")
    for key, value in config_overrides.items():
        config.set("Alerts_Service", key, str(value))
    bot.config = config
    return bot


def _source(bot):
    service = AlertsService(bot)
    return _DummySource(service)


@pytest.mark.unit
class TestRenderTemplate:
    def test_default_preset_used_when_unset(self):
        source = _source(_bot())
        text = source.render(prefix="Warning", title="Storm", date_part="")
        assert text == "Warning: Storm"

    def test_named_preset_selected_by_config(self):
        source = _source(_bot(dummy_template="minimal"))
        text = source.render(prefix="Warning", title="Storm", date_part="")
        assert text == "Storm"

    def test_custom_format_string_used_directly(self):
        source = _source(_bot(dummy_template="!! {title} !!"))
        text = source.render(prefix="Warning", title="Storm", date_part="")
        assert text == "!! Storm !!"

    def test_bad_custom_template_falls_back_to_default_preset(self):
        bot = _bot(dummy_template="{nonexistent_placeholder}")
        source = _source(bot)
        text = source.render(prefix="Warning", title="Storm", date_part="")
        assert text == "Warning: Storm"
        bot.logger.warning.assert_called_once()

    def test_bad_template_warning_logged_only_once(self):
        bot = _bot(dummy_template="{nonexistent_placeholder}")
        source = _source(bot)
        source.render(prefix="Warning", title="Storm", date_part="")
        source.render(prefix="Warning", title="Storm", date_part="")
        assert bot.logger.warning.call_count == 1

    def test_optional_context_precomputed_by_caller(self):
        """Conditional pieces (e.g. an optional date) are pre-assembled by the
        caller into an already-formatted context value, not expressed as
        template-level conditionals -- see base.py's render_template docstring."""
        source = _source(_bot())
        text = source.render(prefix="Warning", title="Storm", date_part=" [10.08]")
        assert text == "Warning: Storm [10.08]"


@pytest.mark.unit
class TestSharedHelpers:
    def test_translate_falls_back_safely_when_bot_has_no_translator(self):
        bot = _bot()
        bot.translator = None
        source = _source(bot)
        assert source._translate("some.key") == "some.key"

    def test_format_dt_short(self):
        source = _source(_bot())
        assert source._format_dt_short("2026-08-10 19:00:00") == "19:00 10.08"

    def test_format_dt_short_missing_returns_none(self):
        source = _source(_bot())
        assert source._format_dt_short(None) is None

    def test_format_dt_short_unparseable_returns_raw(self):
        source = _source(_bot())
        assert source._format_dt_short("not-a-date") == "not-a-date"

    def test_format_date_short(self):
        source = _source(_bot())
        assert source._format_date_short("2026-08-10 19:00:00") == "10.08"

    def test_format_date_short_missing_returns_none(self):
        source = _source(_bot())
        assert source._format_date_short(None) is None
        assert source._format_date_short("garbage") is None
