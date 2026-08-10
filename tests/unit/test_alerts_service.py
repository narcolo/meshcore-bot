"""Unit tests for AlertsService (Phase 2a: IMGW meteo warnings + RSO).

Covers dedup, restart-safety (bot_metadata-backed seen-id persistence), the
per-source alerts/hour cap, flood_scope threading through to send_channel_message,
and message formatting. No network calls -- alert_sources fetch functions are
mocked at the call site.
"""

import configparser
import json
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from modules.i18n import Translator
from modules.service_plugins.alerts_service import (
    METADATA_KEY_IMGW_SEEN,
    METADATA_KEY_RSO_SEEN,
    AlertsService,
)

IMGW_RECORD = {
    "source": "imgw_meteo",
    "external_id": "Wr20260810041907317",
    "type": "weather",
    "severity": "1",
    "area": "Bialystok / powiat bialostocki",
    "title": "Burze",
    "description": "Prognozowane sa lokalne burze z silnym wiatrem i opadami deszczu.",
    "published_at": "2026-08-10 06:19:00",
    "valid_from": "2026-08-10 19:00:00",
    "valid_to": "2026-08-11 03:00:00",
    "url": None,
}

RSO_RECORD = {
    "source": "rso",
    "external_id": "23233017",
    "type": "alert",
    "severity": "2",
    "area": "Podlaskie",
    "title": "ALERT RCB - BURZE/2",
    "description": "Pogoda: dzis i w nocy burze z silnym wiatrem i intensywnymi opadami deszczu.",
    "published_at": "2026-08-10 14:00:52",
    "valid_from": "2026-08-10 13:58:00",
    "valid_to": "2026-08-11 04:00:00",
    "url": None,
}


class _FakeDBManager:
    """Minimal stand-in for DBManager.get_metadata/set_metadata, backed by a dict
    that persists across AlertsService instances (simulates a real restart)."""

    def __init__(self):
        self._store: dict[str, str] = {}

    def get_metadata(self, key):
        return self._store.get(key)

    def set_metadata(self, key, value):
        self._store[key] = value


def _alerts_service_bot(db_manager=None, **config_overrides):
    bot = MagicMock()
    bot.logger = Mock()
    bot.translator = Translator("en")  # real translator, exercises real translation keys
    bot.db_manager = db_manager if db_manager is not None else _FakeDBManager()
    bot.command_manager.send_channel_message = AsyncMock(return_value=True)

    config = configparser.ConfigParser()
    config.add_section("Alerts_Service")
    config.set("Alerts_Service", "enabled", "true")
    config.set("Alerts_Service", "channel", "general")
    for key, value in config_overrides.items():
        config.set("Alerts_Service", key, str(value))
    bot.config = config
    return bot


@pytest.mark.unit
class TestDedup:
    async def test_new_alert_is_sent(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        await service._process_new_alerts(
            "imgw_meteo", [IMGW_RECORD], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=12,
        )
        bot.command_manager.send_channel_message.assert_awaited_once()

    async def test_same_alert_polled_twice_sent_once(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        for _ in range(2):
            await service._process_new_alerts(
                "imgw_meteo", [IMGW_RECORD], service._format_imgw_message,
                send_details=False, max_alerts_per_hour=12,
            )
        bot.command_manager.send_channel_message.assert_awaited_once()

    async def test_no_new_alerts_sends_nothing(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        await service._process_new_alerts(
            "imgw_meteo", [], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=12,
        )
        bot.command_manager.send_channel_message.assert_not_awaited()

    async def test_restart_safety_persisted_id_prevents_resend(self):
        """Simulates a restart: a fresh AlertsService sharing the same db_manager
        must not resend an alert whose id was already persisted."""
        db_manager = _FakeDBManager()
        db_manager.set_metadata(
            METADATA_KEY_IMGW_SEEN, json.dumps([IMGW_RECORD["external_id"]])
        )
        bot = _alerts_service_bot(db_manager=db_manager)
        service = AlertsService(bot)  # loads seen ids from db_manager on construction

        await service._process_new_alerts(
            "imgw_meteo", [IMGW_RECORD], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=12,
        )
        bot.command_manager.send_channel_message.assert_not_awaited()

    async def test_seen_ids_persisted_after_send(self):
        db_manager = _FakeDBManager()
        bot = _alerts_service_bot(db_manager=db_manager)
        service = AlertsService(bot)
        await service._process_new_alerts(
            "rso", [RSO_RECORD], service._format_rso_message,
            send_details=False, max_alerts_per_hour=12,
        )
        persisted = json.loads(db_manager.get_metadata(METADATA_KEY_RSO_SEEN))
        assert RSO_RECORD["external_id"] in persisted


@pytest.mark.unit
class TestRateLimit:
    async def test_alerts_beyond_cap_are_suppressed_not_sent(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        record_2 = dict(IMGW_RECORD, external_id="Wr_second", published_at="2026-08-10 07:00:00")
        await service._process_new_alerts(
            "imgw_meteo", [IMGW_RECORD, record_2], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=1,
        )
        assert bot.command_manager.send_channel_message.await_count == 1

    async def test_suppressed_alert_still_marked_seen(self):
        """A suppressed alert is a hard drop (logged), not a deferred retry."""
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        record_2 = dict(IMGW_RECORD, external_id="Wr_second", published_at="2026-08-10 07:00:00")
        await service._process_new_alerts(
            "imgw_meteo", [IMGW_RECORD, record_2], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=1,
        )
        bot.command_manager.send_channel_message.reset_mock()
        await service._process_new_alerts(
            "imgw_meteo", [IMGW_RECORD, record_2], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=1,
        )
        bot.command_manager.send_channel_message.assert_not_awaited()


@pytest.mark.unit
class TestSendFailureNotLost:
    """Regression coverage for a real bug caught in production: a burst of new
    alerts collided with the bot's own global TX rate limiter
    (send_channel_message returning False), and the old code counted/marked
    every one of them as sent regardless -- 10 of 11 first-run alerts were
    silently lost. A failed send must not be marked seen, so it is retried."""

    async def test_failed_send_is_not_marked_seen(self):
        bot = _alerts_service_bot()
        bot.command_manager.send_channel_message = AsyncMock(return_value=False)
        service = AlertsService(bot)
        await service._process_new_alerts(
            "imgw_meteo", [IMGW_RECORD], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=12,
        )
        assert IMGW_RECORD["external_id"] not in service._seen_ids["imgw_meteo"]

    async def test_failed_send_is_retried_on_next_poll(self):
        bot = _alerts_service_bot()
        # Fails the first time, succeeds the second (simulates the rate limiter
        # clearing by the next poll).
        bot.command_manager.send_channel_message = AsyncMock(side_effect=[False, True])
        service = AlertsService(bot)

        await service._process_new_alerts(
            "imgw_meteo", [IMGW_RECORD], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=12,
        )
        await service._process_new_alerts(
            "imgw_meteo", [IMGW_RECORD], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=12,
        )
        assert bot.command_manager.send_channel_message.await_count == 2
        assert IMGW_RECORD["external_id"] in service._seen_ids["imgw_meteo"]

    async def test_send_details_followup_skipped_when_primary_send_fails(self):
        bot = _alerts_service_bot()
        bot.command_manager.send_channel_message = AsyncMock(return_value=False)
        service = AlertsService(bot)
        await service._process_new_alerts(
            "imgw_meteo", [IMGW_RECORD], service._format_imgw_message,
            send_details=True, max_alerts_per_hour=12,
        )
        # Only the one (failed) primary-message attempt -- no follow-up detail send.
        assert bot.command_manager.send_channel_message.await_count == 1


@pytest.mark.unit
class TestSendPacing:
    """A burst of several new alerts in one poll must be paced to the bot's own
    global TX rate limit, not fired back-to-back (which is exactly what caused
    the send-failure bug above in the first place)."""

    async def test_sleeps_between_sends_using_configured_rate_limit(self, monkeypatch):
        bot = _alerts_service_bot()
        bot.config.add_section("Bot")
        bot.config.set("Bot", "rate_limit_seconds", "3")
        service = AlertsService(bot)

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        monkeypatch.setattr(
            "modules.service_plugins.alerts_service.asyncio.sleep", fake_sleep
        )

        record_2 = dict(RSO_RECORD, external_id="second", published_at="2026-08-10 15:00:00")
        await service._process_new_alerts(
            "rso", [RSO_RECORD, record_2], service._format_rso_message,
            send_details=False, max_alerts_per_hour=12,
        )
        assert sleep_calls == [3.3]  # configured 3s + 0.3s margin, once (before 2nd send)

    async def test_no_sleep_for_a_single_alert(self, monkeypatch):
        bot = _alerts_service_bot()
        service = AlertsService(bot)

        async def fail_if_called(seconds):
            raise AssertionError("should not sleep when only one alert is sent")

        monkeypatch.setattr(
            "modules.service_plugins.alerts_service.asyncio.sleep", fail_if_called
        )
        await service._process_new_alerts(
            "imgw_meteo", [IMGW_RECORD], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=12,
        )
        bot.command_manager.send_channel_message.assert_awaited_once()

    async def test_rate_capped_alert_does_not_consume_a_pacing_sleep(self, monkeypatch):
        """Suppressed-by-cap alerts hit `continue` before the pacing sleep --
        only real send attempts after the first should pace."""
        bot = _alerts_service_bot()
        service = AlertsService(bot)

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        monkeypatch.setattr(
            "modules.service_plugins.alerts_service.asyncio.sleep", fake_sleep
        )
        record_2 = dict(IMGW_RECORD, external_id="second", published_at="2026-08-10 07:00:00")
        await service._process_new_alerts(
            "imgw_meteo", [IMGW_RECORD, record_2], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=1,  # only 1 allowed -> 2nd is capped, not sent
        )
        assert sleep_calls == []


@pytest.mark.unit
class TestFloodScope:
    async def test_flood_scope_passed_to_send_channel_message(self):
        bot = _alerts_service_bot(flood_scope="pl-podlasie")
        service = AlertsService(bot)
        await service._process_new_alerts(
            "imgw_meteo", [IMGW_RECORD], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=12,
        )
        _, kwargs = bot.command_manager.send_channel_message.call_args
        assert kwargs["scope"] == "pl-podlasie"

    async def test_empty_flood_scope_passes_none(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        await service._process_new_alerts(
            "imgw_meteo", [IMGW_RECORD], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=12,
        )
        _, kwargs = bot.command_manager.send_channel_message.call_args
        assert kwargs["scope"] is None


@pytest.mark.unit
class TestSendDetails:
    async def test_send_details_false_sends_one_message(self):
        bot = _alerts_service_bot(imgw_meteo_send_details="false")
        service = AlertsService(bot)
        await service._process_new_alerts(
            "imgw_meteo", [IMGW_RECORD], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=12,
        )
        assert bot.command_manager.send_channel_message.await_count == 1

    async def test_send_details_true_sends_followup_message(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        await service._process_new_alerts(
            "imgw_meteo", [IMGW_RECORD], service._format_imgw_message,
            send_details=True, max_alerts_per_hour=12,
        )
        assert bot.command_manager.send_channel_message.await_count == 2
        second_call_text = bot.command_manager.send_channel_message.call_args_list[1].args[1]
        assert second_call_text.startswith("Prognozowane")


@pytest.mark.unit
class TestMessageFormatting:
    def test_imgw_message_is_compact_and_contains_key_fields(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        text = service._format_imgw_message(IMGW_RECORD)
        assert len(text) <= 140
        assert "Burze" in text
        assert "st.1" in text
        assert "03:00 11.08" in text

    def test_rso_message_is_compact_and_contains_title(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        text = service._format_rso_message(RSO_RECORD)
        assert len(text) <= 140
        assert "ALERT RCB - BURZE/2" in text

    def test_long_description_does_not_blow_budget(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        long_record = dict(RSO_RECORD, description="X" * 300)
        text = service._format_rso_message(long_record)
        assert len(text) <= 140

    def test_translate_falls_back_safely_when_bot_has_no_translator(self):
        """A service plugin has no BaseCommand.translate() helper; when the bot
        object doesn't expose .translator at all, formatting must not crash."""
        bot = _alerts_service_bot()
        bot.translator = None
        service = AlertsService(bot)
        text = service._format_imgw_message(IMGW_RECORD)
        assert "Burze" in text  # still produces a usable message, just untranslated


@pytest.mark.unit
class TestConfigDefaults:
    def test_defaults_match_spec(self):
        """Default poll interval 300s, send_details defaults false, per requirements."""
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        assert service.imgw_poll_interval == 300
        assert service.rso_poll_interval == 300
        assert service.imgw_send_details is False
        assert service.rso_send_details is False
        assert service.imgw_enabled is True
        assert service.rso_enabled is True

    async def test_imgw_and_rso_independent_pollers(self):
        """Starting the service creates one task per enabled source."""
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        await service.start()
        try:
            assert len(service._tasks) == 2
        finally:
            await service.stop()
