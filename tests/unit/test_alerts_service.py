"""Unit tests for AlertsService.

Phase 2a (IMGW meteo warnings + RSO): dedup, restart-safety (bot_metadata-backed
seen-id persistence), the per-source alerts/hour cap, staleness filtering,
multi-message chunking of long alerts, flood_scope threading, and message
formatting.

Phase 2b:
- GIOS air quality: threshold-crossing with hysteresis (_apply_threshold_state)
  instead of event dedup -- alert on crossing up, stay silent while still elevated
  (until the renotify interval), alert once on crossing back down.
- PAA radiation: a scheduled digest (_daily_schedule_loop), not threshold-gated --
  fires unconditionally at fixed times of day (default 06:00/18:00), one combined
  message for all configured stations, restart-safe (won't re-fire a slot that
  already fired today).

No network calls -- alert_sources fetch functions are mocked at the call site.
"""

import configparser
import json
from datetime import datetime, time, timedelta
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from modules.command_manager import CommandManager
from modules.i18n import Translator
from modules.service_plugins.alerts_service import (
    MESSAGE_CHUNK_MAX_BYTES,
    METADATA_KEY_GIOS_STATE,
    METADATA_KEY_IMGW_SEEN,
    METADATA_KEY_PAA_LAST_SLOT,
    METADATA_KEY_RSO_SEEN,
    AlertsService,
)


def _recent(hours_offset: float) -> str:
    """Timestamp relative to whenever the test suite actually runs -- fixtures
    must never hardcode an absolute date, or they silently start failing
    _is_stale's 24h check the moment a real day boundary passes (this bit us:
    see the 2026-08-11 regression where every IMGW/RSO fixture went stale
    overnight purely because the calendar moved, not because of a code bug)."""
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

GIOS_GOOD_READING = {"station_id": 11174, "category": "Bardzo dobry", "computed_at": "2026-08-10 18:20:20"}
GIOS_BAD_READING = {"station_id": 11174, "category": "Bardzo zły", "computed_at": "2026-08-10 18:20:20"}

PAA_FRESH_READING = {
    "station": "Suwałki", "value": 0.084, "unit": "µSv/h", "timestamp": "2026-08-10 20:00"
}
PAA_STALE_READING = {
    "station": "Białystok", "value": 0.064, "unit": "µSv/h", "timestamp": "2026-08-04 16:00"
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
        record_2 = dict(IMGW_RECORD, external_id="Wr_second", published_at=_recent(-1.5))
        await service._process_new_alerts(
            "imgw_meteo", [IMGW_RECORD, record_2], service._format_imgw_message,
            send_details=False, max_alerts_per_hour=1, max_age_hours=24,
        )
        assert bot.command_manager.send_channel_messages_chunked.await_count == 1

    async def test_suppressed_alert_still_marked_seen(self):
        """A suppressed alert is a hard drop (logged), not a deferred retry."""
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        record_2 = dict(IMGW_RECORD, external_id="Wr_second", published_at=_recent(-1.5))
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
        expected_valid_to = datetime.strptime(
            IMGW_RECORD["valid_to"], "%Y-%m-%d %H:%M:%S"
        ).strftime("%H:%M %d.%m")
        assert expected_valid_to in text
        expected_date = datetime.strptime(
            IMGW_RECORD["published_at"], "%Y-%m-%d %H:%M:%S"
        ).strftime("%d.%m")
        assert f"[{expected_date}]" in text  # published_at date, distinct from the valid_to time

    def test_rso_message_contains_title_and_full_description(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        text = service._format_rso_message(RSO_RECORD)
        assert "ALERT RCB - BURZE/2" in text
        expected_date = datetime.strptime(
            RSO_RECORD["published_at"], "%Y-%m-%d %H:%M:%S"
        ).strftime("%d.%m")
        assert f"[{expected_date}]" in text  # published_at date
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


def _fresh_paa_timestamp(minutes_ago: float = 5) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M")


@pytest.mark.unit
class TestGiosThreshold:
    """Threshold-crossing with hysteresis, not event dedup -- see
    AlertsService._apply_threshold_state."""

    async def test_no_alert_when_below_threshold(self):
        bot = _alerts_service_bot(gios_alert_category="Zły")
        service = AlertsService(bot)
        with patch(
            "modules.service_plugins.alerts_service.alert_sources.fetch_gios_aqindex",
            return_value=GIOS_GOOD_READING,
        ):
            await service._check_gios()
        bot.command_manager.send_channel_messages_chunked.assert_not_awaited()

    async def test_alert_when_at_or_above_threshold(self):
        bot = _alerts_service_bot(gios_alert_category="Zły")
        service = AlertsService(bot)
        with patch(
            "modules.service_plugins.alerts_service.alert_sources.fetch_gios_aqindex",
            return_value=GIOS_BAD_READING,
        ):
            await service._check_gios()
        bot.command_manager.send_channel_messages_chunked.assert_awaited_once()
        chunks = _sent_chunk_lists(bot)[0]
        assert "Bardzo zły" in "".join(chunks)

    async def test_no_repeat_alert_before_renotify_interval(self):
        bot = _alerts_service_bot(gios_alert_category="Zły", gios_renotify_hours="6")
        service = AlertsService(bot)
        with patch(
            "modules.service_plugins.alerts_service.alert_sources.fetch_gios_aqindex",
            return_value=GIOS_BAD_READING,
        ):
            await service._check_gios()  # crosses into elevated -> alerts
            bot.command_manager.send_channel_messages_chunked.reset_mock()
            await service._check_gios()  # still elevated, renotify not due -> silent
        bot.command_manager.send_channel_messages_chunked.assert_not_awaited()

    async def test_repeat_alert_after_renotify_interval_elapses(self):
        bot = _alerts_service_bot(gios_alert_category="Zły", gios_renotify_hours="6")
        service = AlertsService(bot)
        with patch(
            "modules.service_plugins.alerts_service.alert_sources.fetch_gios_aqindex",
            return_value=GIOS_BAD_READING,
        ):
            await service._check_gios()
            # Backdate the last notification past the renotify window.
            service._gios_state["11174"]["last_notified_at"] = (
                datetime.now() - timedelta(hours=7)
            ).isoformat()
            bot.command_manager.send_channel_messages_chunked.reset_mock()
            await service._check_gios()
        bot.command_manager.send_channel_messages_chunked.assert_awaited_once()

    async def test_back_to_normal_message_on_recovery(self):
        bot = _alerts_service_bot(gios_alert_category="Zły")
        service = AlertsService(bot)
        service._gios_state["11174"] = {"state": "elevated", "last_notified_at": None}
        with patch(
            "modules.service_plugins.alerts_service.alert_sources.fetch_gios_aqindex",
            return_value=GIOS_GOOD_READING,
        ):
            await service._check_gios()
        bot.command_manager.send_channel_messages_chunked.assert_awaited_once()
        assert service._gios_state["11174"]["state"] == "normal"
        chunks = _sent_chunk_lists(bot)[0]
        assert "Bardzo dobry" in "".join(chunks)

    async def test_no_computable_index_leaves_state_untouched(self):
        """'Brak indeksu' (fetch returns None) -- don't guess either way."""
        bot = _alerts_service_bot(gios_alert_category="Zły")
        service = AlertsService(bot)
        service._gios_state["11174"] = {"state": "elevated", "last_notified_at": "x"}
        with patch(
            "modules.service_plugins.alerts_service.alert_sources.fetch_gios_aqindex",
            return_value=None,
        ):
            await service._check_gios()
        bot.command_manager.send_channel_messages_chunked.assert_not_awaited()
        assert service._gios_state["11174"]["state"] == "elevated"  # unchanged

    async def test_unrecognized_category_is_skipped_safely(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        with patch(
            "modules.service_plugins.alerts_service.alert_sources.fetch_gios_aqindex",
            return_value={"station_id": 11174, "category": "???", "computed_at": "x"},
        ):
            await service._check_gios()  # must not raise
        bot.command_manager.send_channel_messages_chunked.assert_not_awaited()


@pytest.mark.unit
class TestPaaDigest:
    """PAA is a scheduled digest, not threshold-gated -- it always sends exactly
    one combined message per check, regardless of any station's reading."""

    async def test_digest_sent_even_when_all_stations_below_threshold(self):
        bot = _alerts_service_bot(paa_stations="Suwalki", paa_alert_dose_rate_usvh="0.3")
        service = AlertsService(bot)
        reading = dict(PAA_FRESH_READING, timestamp=_fresh_paa_timestamp())
        with patch(
            "modules.service_plugins.alerts_service.alert_sources.fetch_paa_radiation",
            return_value=[reading],
        ):
            await service._check_paa()
        bot.command_manager.send_channel_messages_chunked.assert_awaited_once()

    async def test_digest_combines_all_stations_into_one_message(self):
        bot = _alerts_service_bot(paa_stations="Suwalki,Siemiatycze")
        service = AlertsService(bot)
        readings = [
            dict(PAA_FRESH_READING, station="Suwałki", value=0.084, timestamp=_fresh_paa_timestamp()),
            dict(PAA_FRESH_READING, station="Siemiatycze", value=0.068, timestamp=_fresh_paa_timestamp()),
        ]
        with patch(
            "modules.service_plugins.alerts_service.alert_sources.fetch_paa_radiation",
            return_value=readings,
        ):
            await service._check_paa()
        assert bot.command_manager.send_channel_messages_chunked.await_count == 1
        sent_text = "".join(_sent_chunk_lists(bot)[0])
        assert "Suwałki" in sent_text
        assert "Siemiatycze" in sent_text

    async def test_stations_appear_in_configured_order(self):
        bot = _alerts_service_bot(paa_stations="Siemiatycze,Bialystok,Suwalki")
        service = AlertsService(bot)
        readings = [
            dict(PAA_FRESH_READING, station="Suwałki", value=0.084, timestamp=_fresh_paa_timestamp()),
            dict(PAA_FRESH_READING, station="Siemiatycze", value=0.068, timestamp=_fresh_paa_timestamp()),
            dict(PAA_FRESH_READING, station="Białystok", value=0.064, timestamp=_fresh_paa_timestamp()),
        ]
        with patch(
            "modules.service_plugins.alerts_service.alert_sources.fetch_paa_radiation",
            return_value=readings,
        ):
            await service._check_paa()
        sent_text = "".join(_sent_chunk_lists(bot)[0])
        assert sent_text.index("Siemiatycze") < sent_text.index("Białystok") < sent_text.index("Suwałki")

    async def test_warning_marker_shown_above_threshold(self):
        bot = _alerts_service_bot(paa_stations="Suwalki", paa_alert_dose_rate_usvh="0.01")
        service = AlertsService(bot)
        reading = dict(PAA_FRESH_READING, timestamp=_fresh_paa_timestamp())  # 0.084 >= 0.01
        with patch(
            "modules.service_plugins.alerts_service.alert_sources.fetch_paa_radiation",
            return_value=[reading],
        ):
            await service._check_paa()
        sent_text = "".join(_sent_chunk_lists(bot)[0])
        assert "⚠️" in sent_text

    async def test_no_warning_marker_below_threshold(self):
        bot = _alerts_service_bot(paa_stations="Suwalki", paa_alert_dose_rate_usvh="0.3")
        service = AlertsService(bot)
        reading = dict(PAA_FRESH_READING, timestamp=_fresh_paa_timestamp())  # 0.084 < 0.3
        with patch(
            "modules.service_plugins.alerts_service.alert_sources.fetch_paa_radiation",
            return_value=[reading],
        ):
            await service._check_paa()
        sent_text = "".join(_sent_chunk_lists(bot)[0])
        assert "⚠️" not in sent_text

    async def test_stale_station_shown_as_no_data_not_omitted(self):
        bot = _alerts_service_bot(paa_stations="Bialystok", paa_max_reading_age_hours="6")
        service = AlertsService(bot)
        with patch(
            "modules.service_plugins.alerts_service.alert_sources.fetch_paa_radiation",
            return_value=[PAA_STALE_READING],  # 6 days old
        ):
            await service._check_paa()
        sent_text = "".join(_sent_chunk_lists(bot)[0])
        assert "Bialystok" in sent_text  # configured name, since no fresh real reading

    async def test_missing_configured_station_shown_as_no_data_and_warns(self):
        bot = _alerts_service_bot(paa_stations="Nonexistent Station")
        service = AlertsService(bot)
        with patch(
            "modules.service_plugins.alerts_service.alert_sources.fetch_paa_radiation",
            return_value=[],
        ):
            await service._check_paa()  # must not raise
        bot.command_manager.send_channel_messages_chunked.assert_awaited_once()  # still sends the digest
        bot.logger.warning.assert_called()

    async def test_digest_has_no_persisted_per_station_state(self):
        """Unlike GIOS, PAA is stateless per-station -- there's no hysteresis to
        get stuck in, so nothing here should touch bot_metadata beyond the
        schedule's last-fired-slot marker (covered in TestPaaSchedule)."""
        db_manager = _FakeDBManager()
        bot = _alerts_service_bot(db_manager=db_manager, paa_stations="Suwalki")
        service = AlertsService(bot)
        reading = dict(PAA_FRESH_READING, timestamp=_fresh_paa_timestamp())
        with patch(
            "modules.service_plugins.alerts_service.alert_sources.fetch_paa_radiation",
            return_value=[reading],
        ):
            await service._check_paa()
        assert db_manager.get_metadata(METADATA_KEY_PAA_LAST_SLOT) is None  # _check_paa doesn't set it


@pytest.mark.unit
class TestGiosStatePersistence:
    async def test_gios_state_persisted_after_transition(self):
        db_manager = _FakeDBManager()
        bot = _alerts_service_bot(db_manager=db_manager, gios_alert_category="Zły")
        service = AlertsService(bot)
        with patch(
            "modules.service_plugins.alerts_service.alert_sources.fetch_gios_aqindex",
            return_value=GIOS_BAD_READING,
        ):
            await service._check_gios()
        persisted = json.loads(db_manager.get_metadata(METADATA_KEY_GIOS_STATE))
        assert persisted["11174"]["state"] == "elevated"

    async def test_gios_state_survives_restart(self):
        """A fresh AlertsService sharing the same db_manager must not re-alert for
        a station that's already in the elevated state."""
        db_manager = _FakeDBManager()
        bot = _alerts_service_bot(db_manager=db_manager, gios_alert_category="Zły")
        service = AlertsService(bot)
        with patch(
            "modules.service_plugins.alerts_service.alert_sources.fetch_gios_aqindex",
            return_value=GIOS_BAD_READING,
        ):
            await service._check_gios()

        bot2 = _alerts_service_bot(db_manager=db_manager, gios_alert_category="Zły")
        service2 = AlertsService(bot2)
        assert service2._gios_state["11174"]["state"] == "elevated"
        with patch(
            "modules.service_plugins.alerts_service.alert_sources.fetch_gios_aqindex",
            return_value=GIOS_BAD_READING,
        ):
            await service2._check_gios()
        bot2.command_manager.send_channel_messages_chunked.assert_not_awaited()


@pytest.mark.unit
class TestPaaSchedule:
    """_daily_schedule_loop: fixed wall-clock times, not an interval -- fires at
    most once per configured slot per day, restart-safe via a persisted
    last-fired-slot marker."""

    def test_parses_valid_times(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        assert service._parse_schedule_times("06:00,18:00") == [time(6, 0), time(18, 0)]

    def test_sorts_and_dedupes(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        assert service._parse_schedule_times("18:00,06:00,06:00") == [time(6, 0), time(18, 0)]

    def test_invalid_entries_skipped_with_warning(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        result = service._parse_schedule_times("06:00,not-a-time,18:00")
        assert result == [time(6, 0), time(18, 0)]
        bot.logger.warning.assert_called()

    def test_falls_back_to_default_when_nothing_valid(self):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        assert service._parse_schedule_times("garbage") == [time(6, 0), time(18, 0)]

    async def test_fires_for_a_slot_already_passed_today_on_first_run(self, monkeypatch):
        """Startup catch-up: if the bot starts after a slot already passed today
        and it's never fired, fire now rather than waiting until tomorrow."""
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        fired = []

        async def fake_check():
            fired.append(1)

        async def fake_sleep(seconds):
            service._running = False

        monkeypatch.setattr("modules.service_plugins.alerts_service.asyncio.sleep", fake_sleep)
        service._running = True
        # Midnight has always already passed "today" by construction, regardless
        # of when this test actually runs -- deterministic without freezing time.
        await service._daily_schedule_loop("test", fake_check, [time(0, 0)], "test_slot_key_1")
        assert len(fired) == 1

    async def test_does_not_refire_the_same_slot_twice(self, monkeypatch):
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        fired = []

        async def fake_check():
            fired.append(1)

        call_count = {"n": 0}

        async def fake_sleep(seconds):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                service._running = False

        monkeypatch.setattr("modules.service_plugins.alerts_service.asyncio.sleep", fake_sleep)
        service._running = True
        await service._daily_schedule_loop("test", fake_check, [time(0, 0)], "test_slot_key_2")
        assert len(fired) == 1  # second loop iteration sees the same already-fired slot

    async def test_restart_with_persisted_slot_does_not_refire(self, monkeypatch):
        db_manager = _FakeDBManager()
        bot = _alerts_service_bot(db_manager=db_manager)
        service = AlertsService(bot)
        # Pre-fire today's midnight slot, simulating a previous process run.
        today_midnight = datetime.combine(datetime.now().date(), time(0, 0))
        service._persist_last_slot("test_slot_key_3", today_midnight.isoformat())

        fired = []

        async def fake_check():
            fired.append(1)

        async def fake_sleep(seconds):
            service._running = False

        monkeypatch.setattr("modules.service_plugins.alerts_service.asyncio.sleep", fake_sleep)
        service._running = True
        await service._daily_schedule_loop("test", fake_check, [time(0, 0)], "test_slot_key_3")
        assert fired == []

    async def test_sleeps_until_next_slot_boundary(self, monkeypatch):
        bot = _alerts_service_bot()
        service = AlertsService(bot)

        async def fake_check():
            pass

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)
            service._running = False

        monkeypatch.setattr("modules.service_plugins.alerts_service.asyncio.sleep", fake_sleep)
        service._running = True
        await service._daily_schedule_loop(
            "test", fake_check, [time(0, 0), time(23, 59)], "test_slot_key_4"
        )
        assert len(sleep_calls) == 1
        assert 0 < sleep_calls[0] <= 24 * 3600


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
        # GIOS/PAA (Phase 2b) default off -- opt-in, unlike IMGW/RSO
        assert service.gios_enabled is False
        assert service.paa_enabled is False
        assert service.gios_poll_interval == 900
        assert service.gios_station_id == 11174
        assert service.gios_alert_category == "Zły"
        assert service.gios_renotify_hours == 6.0
        assert service.paa_schedule_times == [time(6, 0), time(18, 0)]
        assert service.paa_stations == ["Bialystok", "Suwalki", "Siemiatycze"]
        assert service.paa_alert_dose_rate_usvh == 0.3
        assert service.paa_max_reading_age_hours == 6.0

    async def test_imgw_and_rso_independent_pollers(self):
        """Starting the service creates one task per enabled source."""
        bot = _alerts_service_bot()
        service = AlertsService(bot)
        await service.start()
        try:
            assert len(service._tasks) == 2
        finally:
            await service.stop()

    async def test_all_four_sources_independent_pollers_when_enabled(self):
        bot = _alerts_service_bot(gios_enabled="true", paa_enabled="true")
        service = AlertsService(bot)
        await service.start()
        try:
            assert len(service._tasks) == 4
        finally:
            await service.stop()
