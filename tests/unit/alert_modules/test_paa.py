"""Unit tests specific to PaaSource: schedule-time parsing, the combined digest
(always sent, not threshold-gated), station resolution/staleness/ordering, and
template presets.
"""

import configparser
from datetime import datetime, time, timedelta
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from modules.command_manager import CommandManager
from modules.i18n import Translator
from modules.service_plugins.alert_modules.paa import PaaSource
from modules.service_plugins.alerts_service import AlertsService

PAA_FRESH_READING = {
    "station": "Suwałki", "value": 0.084, "unit": "µSv/h", "timestamp": "2026-08-10 20:00"
}
PAA_STALE_READING = {
    "station": "Białystok", "value": 0.064, "unit": "µSv/h", "timestamp": "2026-08-04 16:00"
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
    return PaaSource(AlertsService(bot))


def _sent_chunk_lists(bot) -> list[list[str]]:
    return [call.args[1] for call in bot.command_manager.send_channel_messages_chunked.call_args_list]


def _fresh_paa_timestamp(minutes_ago: float = 5) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M")


@pytest.mark.unit
class TestScheduleParsing:
    def test_parses_valid_times(self):
        source = _source(_bot())
        assert source._parse_schedule_times("06:00,18:00") == [time(6, 0), time(18, 0)]

    def test_sorts_and_dedupes(self):
        source = _source(_bot())
        assert source._parse_schedule_times("18:00,06:00,06:00") == [time(6, 0), time(18, 0)]

    def test_invalid_entries_skipped_with_warning(self):
        bot = _bot()
        source = _source(bot)
        result = source._parse_schedule_times("06:00,not-a-time,18:00")
        assert result == [time(6, 0), time(18, 0)]
        bot.logger.warning.assert_called()

    def test_falls_back_to_default_when_nothing_valid(self):
        source = _source(_bot())
        assert source._parse_schedule_times("garbage") == [time(6, 0), time(18, 0)]

    def test_schedule_times_reads_config(self):
        source = _source(_bot(paa_schedule_times="09:00"))
        assert source.schedule_times() == [time(9, 0)]


@pytest.mark.unit
class TestDigest:
    """PAA is a scheduled digest, not threshold-gated -- it always sends exactly
    one combined message per check, regardless of any station's reading."""

    async def test_digest_sent_even_when_all_stations_below_threshold(self):
        bot = _bot(paa_stations="Suwalki", paa_alert_dose_rate_usvh="0.3")
        source = _source(bot)
        reading = dict(PAA_FRESH_READING, timestamp=_fresh_paa_timestamp())
        with patch(
            "modules.service_plugins.alert_modules.paa.alert_sources.fetch_paa_radiation",
            return_value=[reading],
        ):
            await source.check()
        bot.command_manager.send_channel_messages_chunked.assert_awaited_once()

    async def test_digest_combines_all_stations_into_one_message(self):
        bot = _bot(paa_stations="Suwalki,Siemiatycze")
        source = _source(bot)
        readings = [
            dict(PAA_FRESH_READING, station="Suwałki", value=0.084, timestamp=_fresh_paa_timestamp()),
            dict(PAA_FRESH_READING, station="Siemiatycze", value=0.068, timestamp=_fresh_paa_timestamp()),
        ]
        with patch(
            "modules.service_plugins.alert_modules.paa.alert_sources.fetch_paa_radiation",
            return_value=readings,
        ):
            await source.check()
        assert bot.command_manager.send_channel_messages_chunked.await_count == 1
        sent_text = "".join(_sent_chunk_lists(bot)[0])
        assert "Suwałki" in sent_text
        assert "Siemiatycze" in sent_text

    async def test_stations_appear_in_configured_order(self):
        bot = _bot(paa_stations="Siemiatycze,Bialystok,Suwalki")
        source = _source(bot)
        readings = [
            dict(PAA_FRESH_READING, station="Suwałki", value=0.084, timestamp=_fresh_paa_timestamp()),
            dict(PAA_FRESH_READING, station="Siemiatycze", value=0.068, timestamp=_fresh_paa_timestamp()),
            dict(PAA_FRESH_READING, station="Białystok", value=0.064, timestamp=_fresh_paa_timestamp()),
        ]
        with patch(
            "modules.service_plugins.alert_modules.paa.alert_sources.fetch_paa_radiation",
            return_value=readings,
        ):
            await source.check()
        sent_text = "".join(_sent_chunk_lists(bot)[0])
        assert sent_text.index("Siemiatycze") < sent_text.index("Białystok") < sent_text.index("Suwałki")

    async def test_warning_marker_shown_above_threshold(self):
        bot = _bot(paa_stations="Suwalki", paa_alert_dose_rate_usvh="0.01")
        source = _source(bot)
        reading = dict(PAA_FRESH_READING, timestamp=_fresh_paa_timestamp())  # 0.084 >= 0.01
        with patch(
            "modules.service_plugins.alert_modules.paa.alert_sources.fetch_paa_radiation",
            return_value=[reading],
        ):
            await source.check()
        sent_text = "".join(_sent_chunk_lists(bot)[0])
        assert "⚠️" in sent_text

    async def test_no_warning_marker_below_threshold(self):
        bot = _bot(paa_stations="Suwalki", paa_alert_dose_rate_usvh="0.3")
        source = _source(bot)
        reading = dict(PAA_FRESH_READING, timestamp=_fresh_paa_timestamp())  # 0.084 < 0.3
        with patch(
            "modules.service_plugins.alert_modules.paa.alert_sources.fetch_paa_radiation",
            return_value=[reading],
        ):
            await source.check()
        sent_text = "".join(_sent_chunk_lists(bot)[0])
        assert "⚠️" not in sent_text

    async def test_stale_station_shown_as_no_data_not_omitted(self):
        bot = _bot(paa_stations="Bialystok", paa_max_reading_age_hours="6")
        source = _source(bot)
        with patch(
            "modules.service_plugins.alert_modules.paa.alert_sources.fetch_paa_radiation",
            return_value=[PAA_STALE_READING],  # 6 days old
        ):
            await source.check()
        sent_text = "".join(_sent_chunk_lists(bot)[0])
        assert "Bialystok" in sent_text  # configured name, since no fresh real reading

    async def test_missing_configured_station_shown_as_no_data_and_warns(self):
        bot = _bot(paa_stations="Nonexistent Station")
        source = _source(bot)
        with patch(
            "modules.service_plugins.alert_modules.paa.alert_sources.fetch_paa_radiation",
            return_value=[],
        ):
            await source.check()  # must not raise
        bot.command_manager.send_channel_messages_chunked.assert_awaited_once()  # still sends the digest
        bot.logger.warning.assert_called()

    async def test_request_failure_is_caught_and_logged(self):
        import requests

        bot = _bot(paa_stations="Suwalki")
        source = _source(bot)
        with patch(
            "modules.service_plugins.alert_modules.paa.alert_sources.fetch_paa_radiation",
            side_effect=requests.exceptions.RequestException("boom"),
        ):
            await source.check()  # must not raise
        bot.command_manager.send_channel_messages_chunked.assert_not_awaited()
        bot.logger.warning.assert_called()

    async def test_digest_has_no_persisted_state(self):
        """PAA is stateless per-station -- there's no hysteresis to get stuck
        in, so a check() shouldn't touch bot_metadata at all (the schedule's
        last-fired-slot marker is written by AlertsService._daily_schedule_loop,
        not by PaaSource itself)."""
        db_manager = _FakeDBManager()
        bot = _bot(db_manager=db_manager, paa_stations="Suwalki")
        source = _source(bot)
        reading = dict(PAA_FRESH_READING, timestamp=_fresh_paa_timestamp())
        with patch(
            "modules.service_plugins.alert_modules.paa.alert_sources.fetch_paa_radiation",
            return_value=[reading],
        ):
            await source.check()
        assert db_manager.get_metadata("alerts_paa_last_slot") is None


@pytest.mark.unit
class TestTemplates:
    def test_custom_template(self):
        bot = _bot(paa_template="Radiation @ {when}: {stations}")
        source = _source(bot)
        text = source._format_digest({"Suwalki": dict(PAA_FRESH_READING, value=0.05)})
        assert text.startswith("Radiation @ ")
        assert "Suwałki: 0.05" in text


@pytest.mark.unit
class TestRegionConfig:
    async def test_configured_bbox_is_passed_to_fetch(self):
        custom_bbox = "14.0,49.0,24.2,55.0,EPSG:4326"
        bot = _bot(paa_stations="Suwalki", paa_bbox=custom_bbox)
        source = _source(bot)
        with patch(
            "modules.service_plugins.alert_modules.paa.alert_sources.fetch_paa_radiation",
            return_value=[],
        ) as mock_fetch:
            await source.check()
        mock_fetch.assert_called_once_with(["Suwalki"], custom_bbox)

    async def test_default_bbox_is_the_podlaskie_box(self):
        from modules.clients import alert_sources

        bot = _bot(paa_stations="Suwalki")
        source = _source(bot)
        with patch(
            "modules.service_plugins.alert_modules.paa.alert_sources.fetch_paa_radiation",
            return_value=[],
        ) as mock_fetch:
            await source.check()
        mock_fetch.assert_called_once_with(["Suwalki"], alert_sources.PAA_PODLASKIE_BBOX)
