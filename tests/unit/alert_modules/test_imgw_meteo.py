"""Unit tests specific to ImgwMeteoSource: message formatting (compact preset =
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
from modules.service_plugins.alert_modules.imgw_meteo import ImgwMeteoSource
from modules.service_plugins.alerts_service import AlertsService


def _recent(hours_offset: float) -> str:
    return (datetime.now() + timedelta(hours=hours_offset)).strftime("%Y-%m-%d %H:%M:%S")


IMGW_RECORD = {
    "source": "imgw_meteo",
    "external_id": "Wr20260810041907317",
    "type": "weather",
    "severity": "1",
    "area": "Bialystok / powiat bialostocki",
    "title": "Burze",
    "description": "Prognozowane sa lokalne burze z silnym wiatrem i opadami deszczu.",
    "published_at": _recent(-2),
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
    return ImgwMeteoSource(AlertsService(bot))


def _sent_chunk_lists(bot) -> list[list[str]]:
    return [call.args[1] for call in bot.command_manager.send_channel_messages_chunked.call_args_list]


@pytest.mark.unit
class TestMessageFormatting:
    def test_message_contains_key_fields(self):
        source = _source(_bot())
        text = source._format_message(IMGW_RECORD)
        assert "Burze" in text
        assert "st.1" in text
        expected_valid_to = datetime.strptime(
            IMGW_RECORD["valid_to"], "%Y-%m-%d %H:%M:%S"
        ).strftime("%H:%M %d.%m")
        assert expected_valid_to in text
        expected_date = datetime.strptime(
            IMGW_RECORD["published_at"], "%Y-%m-%d %H:%M:%S"
        ).strftime("%d.%m")
        assert f"[{expected_date}]" in text  # published_at date, distinct from the valid_to time

    def test_date_omitted_when_published_at_missing(self):
        source = _source(_bot())
        record = dict(IMGW_RECORD, published_at=None)
        text = source._format_message(record)
        assert "[" not in text


@pytest.mark.unit
class TestTemplates:
    def test_minimal_preset(self):
        source = _source(_bot(imgw_meteo_template="minimal"))
        text = source._format_message(IMGW_RECORD)
        assert text == "⚠️ Burze st.1"

    def test_detailed_preset_appends_description(self):
        source = _source(_bot(imgw_meteo_template="detailed"))
        text = source._format_message(IMGW_RECORD)
        assert IMGW_RECORD["description"] in text
        assert text.startswith("⚠️")

    def test_custom_template(self):
        source = _source(_bot(imgw_meteo_template="IMGW: {title}"))
        text = source._format_message(IMGW_RECORD)
        assert text == "IMGW: Burze"


@pytest.mark.unit
class TestCheckIntegration:
    async def test_new_warning_is_sent(self):
        bot = _bot()
        source = _source(bot)
        with patch(
            "modules.service_plugins.alert_modules.imgw_meteo.alert_sources.fetch_imgw_meteo_warnings",
            return_value=[IMGW_RECORD],
        ):
            await source.check()
        bot.command_manager.send_channel_messages_chunked.assert_awaited_once()

    async def test_request_failure_is_caught_and_logged(self):
        import requests

        bot = _bot()
        source = _source(bot)
        with patch(
            "modules.service_plugins.alert_modules.imgw_meteo.alert_sources.fetch_imgw_meteo_warnings",
            side_effect=requests.exceptions.RequestException("boom"),
        ):
            await source.check()  # must not raise
        bot.command_manager.send_channel_messages_chunked.assert_not_awaited()
        bot.logger.warning.assert_called()

    async def test_send_details_sends_full_text_as_followup(self):
        bot = _bot(imgw_meteo_send_details="true")
        source = _source(bot)
        with patch(
            "modules.service_plugins.alert_modules.imgw_meteo.alert_sources.fetch_imgw_meteo_warnings",
            return_value=[IMGW_RECORD],
        ):
            await source.check()
        assert bot.command_manager.send_channel_messages_chunked.await_count == 2
        second_call_text = "".join(_sent_chunk_lists(bot)[1])
        assert second_call_text.startswith("Prognozowane")


@pytest.mark.unit
class TestRegionConfig:
    async def test_configured_teryt_codes_and_area_label_are_passed_to_fetch(self):
        bot = _bot(imgw_meteo_teryt_codes="1465, 1401", imgw_meteo_area_label="Krakow")
        source = _source(bot)
        with patch(
            "modules.service_plugins.alert_modules.imgw_meteo.alert_sources.fetch_imgw_meteo_warnings",
            return_value=[],
        ) as mock_fetch:
            await source.check()
        mock_fetch.assert_called_once_with({"1465", "1401"}, "Krakow")

    async def test_default_teryt_codes_and_area_label_are_bialystok(self):
        bot = _bot()
        source = _source(bot)
        with patch(
            "modules.service_plugins.alert_modules.imgw_meteo.alert_sources.fetch_imgw_meteo_warnings",
            return_value=[],
        ) as mock_fetch:
            await source.check()
        mock_fetch.assert_called_once_with({"2061", "2002"}, "Bialystok / powiat bialostocki")

    def test_area_placeholder_available_in_templates(self):
        source = _source(_bot(imgw_meteo_template="{title} ({area})"))
        record = dict(IMGW_RECORD, area="Krakow")
        text = source._format_message(record)
        assert text == "Burze (Krakow)"
