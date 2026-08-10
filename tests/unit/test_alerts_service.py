"""Unit tests for AlertsService (Phase 2a: IMGW meteo warnings + RSO).

Covers dedup, restart-safety (bot_metadata-backed seen-id persistence), the
per-source alerts/hour cap, staleness filtering, multi-message chunking of long
alerts, flood_scope threading, and message formatting. No network calls --
alert_sources fetch functions are mocked at the call site.
"""

import configparser
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from modules.command_manager import CommandManager
from modules.i18n import Translator
from modules.service_plugins.alerts_service import (
    MESSAGE_CHUNK_MAX_BYTES,
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

    # Real (pure, side-effect-free) chunking logic, so tests exercise the same
    # word/codepoint-safe splitting the bot actually uses -- only the network-ish
    # send itself is mocked.
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


def _sent_chunk_lists(bot) -> list[list[str]]:
    """Every `chunks` list passed to send_channel_messages_chunked, in call order."""
    return [call.args[1] for call in bot.command_manager.send_channel_messages_chunked.call_args_list]


@pytest.mark.unit
class TestDedup:
    async def test_new_alert_is_sent(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        await service._process_new_alerts(
            "imgw_meteo", [IMGW_RECORD], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        bot.command_manager.send_channel_messages_chunked.assert_awaited_once()

    async def test_same_alert_polled_twice_sent_once(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        for _ in range(2):
            await service._process_new_alerts(
                "imgw_meteo", [IMGW_RECORD], service._format_imgw_message,
                send_details=False, max_alerts_per_hour=12, max_age_hours=24,
            )
        bot.command_manager.send_channel_messages_chunked.assert_awaited_once()

    async def test_no_new_alerts_sends_nothing(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        await service._process_new_alerts(
            "imgw_meteo", [], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        bot.command_manager.send_channel_messages_chunked.assert_not_awaited()

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
            send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        bot.command_manager.send_channel_messages_chunked.assert_not_awaited()

    async def test_seen_ids_persisted_after_send(self):
        db_manager = _FakeDBManager()
        bot = _alerts_service_bot(db_manager=db_manager)
        service = AlertsService(bot)
        await service._process_new_alerts(
            "rso", [RSO_RECORD], service._format_rso_message,
            send_details=False, max_alerts_per_hour=12, max_age_hours=24,
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
            send_details=False, max_alerts_per_hour=1, max_age_hours=24,
        )
        assert bot.command_manager.send_channel_messages_chunked.await_count == 1

    async def test_suppressed_alert_still_marked_seen(self):
        """A suppressed alert is a hard drop (logged), not a deferred retry."""
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        record_2 = dict(IMGW_RECORD, external_id="Wr_second", published_at="2026-08-10 07:00:00")
        await service._process_new_alerts(
            "imgw_meteo", [IMGW_RECORD, record_2], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=1, max_age_hours=24,
        )
        bot.command_manager.send_channel_messages_chunked.reset_mock()
        await service._process_new_alerts(
            "imgw_meteo", [IMGW_RECORD, record_2], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=1, max_age_hours=24,
        )
        bot.command_manager.send_channel_messages_chunked.assert_not_awaited()


@pytest.mark.unit
class TestSendFailureNotLost:
    """Regression coverage for a real bug caught in production: a burst of new
    alerts collided with the bot's own global TX rate limiter, and the old code
    counted/marked every one of them as sent regardless -- 10 of 11 first-run
    alerts were silently lost. A failed send must not be marked seen, so it is
    retried. (send_channel_messages_chunked already skips that particular
    limiter for automated services -- see TestSendFailureNotLost docstring in
    the source -- but the send can still fail for other reasons, e.g. the
    per-channel limiter or a radio error, so the safety net stays regardless.)
    """

    async def test_failed_send_is_not_marked_seen(self):
        bot = _alerts_service_bot()
        bot.command_manager.send_channel_messages_chunked = AsyncMock(return_value=False)
        service = AlertsService(bot)
        await service._process_new_alerts(
            "imgw_meteo", [IMGW_RECORD], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        assert IMGW_RECORD["external_id"] not in service._seen_ids["imgw_meteo"]

    async def test_failed_send_is_retried_on_next_poll(self):
        bot = _alerts_service_bot()
        # Fails the first time, succeeds the second (simulates the limiter
        # clearing, or a transient error resolving, by the next poll).
        bot.command_manager.send_channel_messages_chunked = AsyncMock(side_effect=[False, True])
        service = AlertsService(bot)

        await service._process_new_alerts(
            "imgw_meteo", [IMGW_RECORD], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        await service._process_new_alerts(
            "imgw_meteo", [IMGW_RECORD], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        assert bot.command_manager.send_channel_messages_chunked.await_count == 2
        assert IMGW_RECORD["external_id"] in service._seen_ids["imgw_meteo"]

    async def test_send_details_followup_skipped_when_primary_send_fails(self):
        bot = _alerts_service_bot()
        bot.command_manager.send_channel_messages_chunked = AsyncMock(return_value=False)
        service = AlertsService(bot)
        await service._process_new_alerts(
            "imgw_meteo", [IMGW_RECORD], service._format_imgw_message,
            send_details=True, max_alerts_per_hour=12, max_age_hours=24,
        )
        # Only the one (failed) primary-message attempt -- no follow-up detail send.
        assert bot.command_manager.send_channel_messages_chunked.await_count == 1


@pytest.mark.unit
class TestMessageChunking:
    """The actual feature this session added: long alert text is split into
    several messages instead of being truncated with '...'."""

    async def test_short_alert_is_a_single_chunk(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        await service._process_new_alerts(
            "imgw_meteo", [IMGW_RECORD], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        chunk_lists = _sent_chunk_lists(bot)
        assert len(chunk_lists) == 1
        assert len(chunk_lists[0]) == 1  # one message, not split

    async def test_long_rso_description_splits_into_multiple_messages_not_truncated(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        long_description = (
            "Prognozowane sa intensywne opady deszczu oraz silny wiatr w wielu "
            "powiatach wojewodztwa podlaskiego. Mieszkancy powinni zabezpieczyc "
            "mienie, unikac przebywania w lasach i na otwartej przestrzeni, oraz "
            "sledzic komunikaty sluzb ratowniczych. Mozliwe sa lokalne podtopienia "
            "oraz przerwy w dostawie pradu elektrycznego na terenach nizinnych."
        )
        record = dict(RSO_RECORD, description=long_description)
        await service._process_new_alerts(
            "rso", [record], service._format_rso_message,
            send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        chunk_lists = _sent_chunk_lists(bot)
        assert len(chunk_lists) == 1  # one alert -> one chunked call
        chunks = chunk_lists[0]
        assert len(chunks) > 1  # ...but split into multiple messages
        for chunk in chunks:
            assert len(chunk.encode("utf-8")) <= MESSAGE_CHUNK_MAX_BYTES
        # Nothing lost: the full description survives across the chunks (joined
        # with a space, since the chunker drops the boundary space itself -- it's
        # implicit between two separate messages).
        assert long_description in " ".join(chunks)
        assert "..." not in " ".join(chunks)  # no truncation ellipsis anywhere

    async def test_chunk_split_is_word_safe(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        record = dict(RSO_RECORD, description="słowo " * 60)
        await service._process_new_alerts(
            "rso", [record], service._format_rso_message,
            send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        chunks = _sent_chunk_lists(bot)[0]
        assert len(chunks) > 1
        for chunk in chunks:
            assert not chunk.startswith(" ")


@pytest.mark.unit
class TestStaleness:
    """Regression coverage: clearing persisted seen-ids (e.g. after the send-loss
    bug above) must not resurrect week-old news -- only alerts published within
    max_age_hours should ever be sent, dedup-reset or not."""

    def _timestamp(self, hours_ago: float) -> str:
        return (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")

    def test_is_stale_true_for_old_timestamp(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        record = dict(IMGW_RECORD, published_at=self._timestamp(hours_ago=240))  # 10 days
        assert service._is_stale(record, max_age_hours=24) is True

    def test_is_stale_false_for_recent_timestamp(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        record = dict(IMGW_RECORD, published_at=self._timestamp(hours_ago=1))
        assert service._is_stale(record, max_age_hours=24) is False

    def test_is_stale_fails_open_when_no_timestamp(self):
        """No published_at or valid_from at all -- don't suppress on a guess."""
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        record = dict(IMGW_RECORD, published_at=None, valid_from=None)
        assert service._is_stale(record, max_age_hours=24) is False

    def test_is_stale_falls_back_to_valid_from(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        record = dict(IMGW_RECORD, published_at=None, valid_from=self._timestamp(hours_ago=240))
        assert service._is_stale(record, max_age_hours=24) is True

    async def test_stale_alert_is_not_sent(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        old_record = dict(IMGW_RECORD, published_at=self._timestamp(hours_ago=240))
        await service._process_new_alerts(
            "imgw_meteo", [old_record], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        bot.command_manager.send_channel_messages_chunked.assert_not_awaited()

    async def test_stale_alert_is_marked_seen_so_its_not_rechecked_forever(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        old_record = dict(IMGW_RECORD, published_at=self._timestamp(hours_ago=240))
        await service._process_new_alerts(
            "imgw_meteo", [old_record], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        assert old_record["external_id"] in service._seen_ids["imgw_meteo"]

    async def test_fresh_alert_still_sent_alongside_a_stale_one(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        old_record = dict(
            IMGW_RECORD, external_id="old", published_at=self._timestamp(hours_ago=240)
        )
        fresh_record = dict(
            IMGW_RECORD, external_id="fresh", published_at=self._timestamp(hours_ago=1)
        )
        await service._process_new_alerts(
            "imgw_meteo", [old_record, fresh_record], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        bot.command_manager.send_channel_messages_chunked.assert_awaited_once()
        assert "old" in service._seen_ids["imgw_meteo"]
        assert "fresh" in service._seen_ids["imgw_meteo"]


@pytest.mark.unit
class TestFloodScope:
    async def test_flood_scope_passed_to_send(self):
        bot = _alerts_service_bot(flood_scope="pl-podlasie")
        service = AlertsService(bot)
        await service._process_new_alerts(
            "imgw_meteo", [IMGW_RECORD], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        _, kwargs = bot.command_manager.send_channel_messages_chunked.call_args
        assert kwargs["scope"] == "pl-podlasie"

    async def test_empty_flood_scope_passes_none(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        await service._process_new_alerts(
            "imgw_meteo", [IMGW_RECORD], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        _, kwargs = bot.command_manager.send_channel_messages_chunked.call_args
        assert kwargs["scope"] is None


@pytest.mark.unit
class TestSendDetails:
    async def test_send_details_false_sends_one_call(self):
        bot = _alerts_service_bot(imgw_meteo_send_details="false")
        service = AlertsService(bot)
        await service._process_new_alerts(
            "imgw_meteo", [IMGW_RECORD], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        assert bot.command_manager.send_channel_messages_chunked.await_count == 1

    async def test_send_details_true_sends_followup_call_for_imgw(self):
        """IMGW's header never includes the full warning text -- send_details
        triggers a genuine second call carrying it."""
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        await service._process_new_alerts(
            "imgw_meteo", [IMGW_RECORD], service._format_imgw_message,
            send_details=True, max_alerts_per_hour=12, max_age_hours=24,
        )
        assert bot.command_manager.send_channel_messages_chunked.await_count == 2
        second_call_text = "".join(_sent_chunk_lists(bot)[1])
        assert second_call_text.startswith("Prognozowane")

    async def test_send_details_true_does_not_duplicate_for_rso(self):
        """RSO's formatter already merges the description into the primary
        message -- send_details=True must not send it a second time."""
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        await service._process_new_alerts(
            "rso", [RSO_RECORD], service._format_rso_message,
            send_details=True, max_alerts_per_hour=12, max_age_hours=24,
        )
        assert bot.command_manager.send_channel_messages_chunked.await_count == 1


@pytest.mark.unit
class TestMessageFormatting:
    def test_imgw_message_contains_key_fields(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        text = service._format_imgw_message(IMGW_RECORD)
        assert "Burze" in text
        assert "st.1" in text
        assert "03:00 11.08" in text
        assert "[10.08]" in text  # published_at date, distinct from the valid_to time

    def test_rso_message_contains_title_and_full_description(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        text = service._format_rso_message(RSO_RECORD)
        assert "ALERT RCB - BURZE/2" in text
        assert "[10.08]" in text  # published_at date
        assert RSO_RECORD["description"] in text  # full text, not a truncated snippet

    def test_date_omitted_when_published_at_missing(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        record = dict(IMGW_RECORD, published_at=None)
        text = service._format_imgw_message(record)
        assert "[" not in text

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
        assert service.imgw_max_age_hours == 24
        assert service.rso_max_age_hours == 24

    async def test_imgw_and_rso_independent_pollers(self):
        """Starting the service creates one task per enabled source."""
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        await service.start()
        try:
            assert len(service._tasks) == 2
        finally:
            await service.stop()
