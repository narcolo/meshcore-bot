"""Unit tests specific to RsoSource: message formatting (compact preset =
byte-for-byte current behavior), template presets/custom override, and the
check() -> fetch -> _process_new_alerts integration.

Dedup/staleness/rate-cap/chunking machinery itself is covered generically in
test_event_source.py (EventAlertSourceBase) and isn't re-tested here.
"""

import configparser
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from modules.command_manager import CommandManager
from modules.i18n import Translator
from modules.service_plugins.alert_modules.rso import RsoSource
from modules.service_plugins.alerts_service import AlertsService


def _recent(hours_offset: float) -> str:
    return (datetime.now() + timedelta(hours=hours_offset)).strftime("%Y-%m-%d %H:%M:%S")


RSO_RECORD = {
    "source": "rso",
    "external_id": "23233017",
    "type": "alert",
    "severity": "2",
    "area": "Podlaskie",
    "title": "ALERT RCB - BURZE/2",
    "description": "Pogoda: dzis i w nocy burze z silnym wiatrem i intensywnymi opadami deszczu.",
    "published_at": _recent(-1),
    "valid_from": _recent(-1),
    "valid_to": _recent(6),
    "url": None,
}


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
    return RsoSource(AlertsService(bot))


def _sent_chunk_lists(bot) -> list[list[str]]:
    return [call.args[1] for call in bot.command_manager.send_channel_messages_chunked.call_args_list]


@pytest.mark.unit
class TestMessageFormatting:
    def test_message_contains_title_and_full_description(self):
        source = _source(_bot())
        text = source._format_message(RSO_RECORD)
        assert "ALERT RCB - BURZE/2" in text
        expected_date = datetime.strptime(
            RSO_RECORD["published_at"], "%Y-%m-%d %H:%M:%S"
        ).strftime("%d.%m")
        assert f"[{expected_date}]" in text  # published_at date
        assert RSO_RECORD["description"] in text  # full text, not a truncated snippet

    def test_description_omitted_when_equal_to_title(self):
        source = _source(_bot())
        record = dict(RSO_RECORD, description=RSO_RECORD["title"])
        text = source._format_message(record)
        assert text.count(RSO_RECORD["title"]) == 1


@pytest.mark.unit
class TestTemplates:
    def test_minimal_preset(self):
        source = _source(_bot(rso_template="minimal"))
        text = source._format_message(RSO_RECORD)
        assert text == "📢 ALERT RCB - BURZE/2"

    def test_title_only_preset_drops_description(self):
        source = _source(_bot(rso_template="title_only"))
        text = source._format_message(RSO_RECORD)
        assert RSO_RECORD["description"] not in text

    def test_custom_template(self):
        source = _source(_bot(rso_template="RSO: {title}"))
        text = source._format_message(RSO_RECORD)
        assert text == "RSO: ALERT RCB - BURZE/2"


@pytest.mark.unit
class TestCheckIntegration:
    async def test_new_alert_is_sent(self):
        bot = _bot()
        source = _source(bot)
        with patch(
            "modules.service_plugins.alert_modules.rso.alert_sources.fetch_rso_alerts",
            return_value=[RSO_RECORD],
        ):
            await source.check()
        bot.command_manager.send_channel_messages_chunked.assert_awaited_once()

    async def test_request_failure_is_caught_and_logged(self):
        import requests

        bot = _bot()
        source = _source(bot)
        with patch(
            "modules.service_plugins.alert_modules.rso.alert_sources.fetch_rso_alerts",
            side_effect=requests.exceptions.RequestException("boom"),
        ):
            await source.check()  # must not raise
        bot.command_manager.send_channel_messages_chunked.assert_not_awaited()
        bot.logger.warning.assert_called()

    async def test_send_details_does_not_duplicate_merged_description(self):
        """RSO's compact formatter already merges the description into the
        primary message -- send_details=True must not send it a second time."""
        bot = _bot(rso_send_details="true")
        source = _source(bot)
        with patch(
            "modules.service_plugins.alert_modules.rso.alert_sources.fetch_rso_alerts",
            return_value=[RSO_RECORD],
        ):
            await source.check()
        assert bot.command_manager.send_channel_messages_chunked.await_count == 1


@pytest.mark.unit
class TestRegionConfig:
    async def test_configured_wojewodztwo_is_passed_to_fetch(self):
        bot = _bot(rso_wojewodztwo="mazowieckie")
        source = _source(bot)
        with patch(
            "modules.service_plugins.alert_modules.rso.alert_sources.fetch_rso_alerts",
            return_value=[],
        ) as mock_fetch:
            await source.check()
        mock_fetch.assert_called_once_with("mazowieckie")

    async def test_default_wojewodztwo_is_podlaskie(self):
        bot = _bot()
        source = _source(bot)
        with patch(
            "modules.service_plugins.alert_modules.rso.alert_sources.fetch_rso_alerts",
            return_value=[],
        ) as mock_fetch:
            await source.check()
        mock_fetch.assert_called_once_with("podlaskie")
