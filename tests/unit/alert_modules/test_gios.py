"""Unit tests specific to GiosSource: category ranking, message formatting
(compact preset = byte-for-byte current behavior), template presets, and the
check() -> fetch -> threshold-state integration.

The hysteresis state machine itself is covered generically in
test_threshold_source.py (ThresholdAlertSourceBase) and isn't re-tested here.
"""

import configparser
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from modules.command_manager import CommandManager
from modules.i18n import Translator
from modules.service_plugins.alert_modules.gios import GiosSource
from modules.service_plugins.alerts_service import AlertsService

GIOS_GOOD_READING = {"station_id": 11174, "category": "Bardzo dobry", "computed_at": "2026-08-10 18:20:20"}
GIOS_BAD_READING = {"station_id": 11174, "category": "Bardzo zły", "computed_at": "2026-08-10 18:20:20"}


class _FakeDBManager:
    def __init__(self):
        self._store: dict[str, str] = {}

    def get_metadata(self, key):
        return self._store.get(key)

    def set_metadata(self, key, value):
        self._store[key] = value


def _bot(db_manager=None, **config_overrides):
    bot = MagicMock()
    bot.logger = Mock()
    bot.translator = Translator("en")
    bot.db_manager = db_manager if db_manager is not None else _FakeDBManager()
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
    return GiosSource(AlertsService(bot))


def _sent_chunk_lists(bot) -> list[list[str]]:
    return [call.args[1] for call in bot.command_manager.send_channel_messages_chunked.call_args_list]


@pytest.mark.unit
class TestMessageFormatting:
    def test_elevated_message(self):
        source = _source(_bot())
        text = source._format_message(GIOS_BAD_READING, elevated=True)
        assert "Bardzo zły" in text
        assert "🌫️" in text
        assert "[10.08]" in text

    def test_normal_message(self):
        source = _source(_bot())
        text = source._format_message(GIOS_GOOD_READING, elevated=False)
        assert "Bardzo dobry" in text
        assert "✅" in text


@pytest.mark.unit
class TestTemplates:
    def test_minimal_preset(self):
        source = _source(_bot(gios_template="minimal"))
        text = source._format_message(GIOS_BAD_READING, elevated=True)
        assert text == "🌫️ Bardzo zły"

    def test_custom_template(self):
        source = _source(_bot(gios_template="AQI: {category}"))
        text = source._format_message(GIOS_GOOD_READING, elevated=False)
        assert text == "AQI: Bardzo dobry"


@pytest.mark.unit
class TestCheckIntegration:
    async def test_no_alert_when_below_threshold(self):
        bot = _bot(gios_alert_category="Zły")
        source = _source(bot)
        with patch(
            "modules.service_plugins.alert_modules.gios.alert_sources.fetch_gios_aqindex",
            return_value=GIOS_GOOD_READING,
        ):
            await source.check()
        bot.command_manager.send_channel_messages_chunked.assert_not_awaited()

    async def test_alert_when_at_or_above_threshold(self):
        bot = _bot(gios_alert_category="Zły")
        source = _source(bot)
        with patch(
            "modules.service_plugins.alert_modules.gios.alert_sources.fetch_gios_aqindex",
            return_value=GIOS_BAD_READING,
        ):
            await source.check()
        bot.command_manager.send_channel_messages_chunked.assert_awaited_once()
        assert "Bardzo zły" in "".join(_sent_chunk_lists(bot)[0])

    async def test_no_repeat_alert_before_renotify_interval(self):
        bot = _bot(gios_alert_category="Zły", gios_renotify_hours="6")
        source = _source(bot)
        with patch(
            "modules.service_plugins.alert_modules.gios.alert_sources.fetch_gios_aqindex",
            return_value=GIOS_BAD_READING,
        ):
            await source.check()
            bot.command_manager.send_channel_messages_chunked.reset_mock()
            await source.check()
        bot.command_manager.send_channel_messages_chunked.assert_not_awaited()

    async def test_repeat_alert_after_renotify_interval_elapses(self):
        bot = _bot(gios_alert_category="Zły", gios_renotify_hours="6")
        source = _source(bot)
        with patch(
            "modules.service_plugins.alert_modules.gios.alert_sources.fetch_gios_aqindex",
            return_value=GIOS_BAD_READING,
        ):
            await source.check()
            source._state["11174"]["last_notified_at"] = (datetime.now() - timedelta(hours=7)).isoformat()
            bot.command_manager.send_channel_messages_chunked.reset_mock()
            await source.check()
        bot.command_manager.send_channel_messages_chunked.assert_awaited_once()

    async def test_back_to_normal_message_on_recovery(self):
        bot = _bot(gios_alert_category="Zły")
        source = _source(bot)
        source._state["11174"] = {"state": "elevated", "last_notified_at": None}
        with patch(
            "modules.service_plugins.alert_modules.gios.alert_sources.fetch_gios_aqindex",
            return_value=GIOS_GOOD_READING,
        ):
            await source.check()
        bot.command_manager.send_channel_messages_chunked.assert_awaited_once()
        assert source._state["11174"]["state"] == "normal"
        assert "Bardzo dobry" in "".join(_sent_chunk_lists(bot)[0])

    async def test_no_computable_index_leaves_state_untouched(self):
        """'Brak indeksu' (fetch returns None) -- don't guess either way."""
        bot = _bot(gios_alert_category="Zły")
        source = _source(bot)
        source._state["11174"] = {"state": "elevated", "last_notified_at": "x"}
        with patch(
            "modules.service_plugins.alert_modules.gios.alert_sources.fetch_gios_aqindex",
            return_value=None,
        ):
            await source.check()
        bot.command_manager.send_channel_messages_chunked.assert_not_awaited()
        assert source._state["11174"]["state"] == "elevated"  # unchanged

    async def test_unrecognized_category_is_skipped_safely(self):
        bot = _bot()
        source = _source(bot)
        with patch(
            "modules.service_plugins.alert_modules.gios.alert_sources.fetch_gios_aqindex",
            return_value={"station_id": 11174, "category": "???", "computed_at": "x"},
        ):
            await source.check()  # must not raise
        bot.command_manager.send_channel_messages_chunked.assert_not_awaited()

    async def test_request_failure_is_caught_and_logged(self):
        import requests

        bot = _bot()
        source = _source(bot)
        with patch(
            "modules.service_plugins.alert_modules.gios.alert_sources.fetch_gios_aqindex",
            side_effect=requests.exceptions.RequestException("boom"),
        ):
            await source.check()  # must not raise
        bot.command_manager.send_channel_messages_chunked.assert_not_awaited()
        bot.logger.warning.assert_called()


@pytest.mark.unit
class TestStatePersistence:
    async def test_state_persisted_after_transition(self):
        db_manager = _FakeDBManager()
        bot = _bot(db_manager=db_manager, gios_alert_category="Zły")
        source = _source(bot)
        with patch(
            "modules.service_plugins.alert_modules.gios.alert_sources.fetch_gios_aqindex",
            return_value=GIOS_BAD_READING,
        ):
            await source.check()
        persisted = json.loads(db_manager.get_metadata("alerts_gios_threshold_state"))
        assert persisted["11174"]["state"] == "elevated"

    async def test_state_survives_restart(self):
        db_manager = _FakeDBManager()
        bot = _bot(db_manager=db_manager, gios_alert_category="Zły")
        source = _source(bot)
        with patch(
            "modules.service_plugins.alert_modules.gios.alert_sources.fetch_gios_aqindex",
            return_value=GIOS_BAD_READING,
        ):
            await source.check()

        bot2 = _bot(db_manager=db_manager, gios_alert_category="Zły")
        source2 = _source(bot2)
        assert source2._state["11174"]["state"] == "elevated"
        with patch(
            "modules.service_plugins.alert_modules.gios.alert_sources.fetch_gios_aqindex",
            return_value=GIOS_BAD_READING,
        ):
            await source2.check()
        bot2.command_manager.send_channel_messages_chunked.assert_not_awaited()
