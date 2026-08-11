"""Unit tests for EventAlertSourceBase: dedup, restart-safety (bot_metadata-backed
seen-id persistence), the per-source alerts/hour cap, staleness filtering,
multi-message chunking of long alerts, flood_scope threading, and the
send_details follow-up.

This machinery is exercised via a minimal concrete dummy source -- dedup/
staleness/rate-cap/chunking logic all lives in EventAlertSourceBase itself; IMGW
and RSO are just a formatter + config on top of it (see test_imgw_meteo.py /
test_rso.py, added once those source modules exist, for source-specific coverage).
"""

import configparser
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from modules.command_manager import CommandManager
from modules.i18n import Translator
from modules.service_plugins.alert_modules.base import MESSAGE_CHUNK_MAX_BYTES
from modules.service_plugins.alert_modules.event_source import EventAlertSourceBase
from modules.service_plugins.alerts_service import AlertsService


def _recent(hours_offset: float) -> str:
    """Timestamp relative to whenever the test suite actually runs -- fixtures
    must never hardcode an absolute date, or they silently start failing
    _is_stale's max-age check the moment a real day boundary passes."""
    return (datetime.now() + timedelta(hours=hours_offset)).strftime("%Y-%m-%d %H:%M:%S")


RECORD_A = {
    "external_id": "Wr20260810041907317",
    "title": "Burze",
    "description": "Prognozowane sa lokalne burze z silnym wiatrem i opadami deszczu.",
    "published_at": _recent(-2),
    "valid_from": _recent(-1),
    "valid_to": _recent(6),
}


class _FakeDBManager:
    """Minimal stand-in for DBManager.get_metadata/set_metadata, backed by a dict
    that persists across instances (simulates a real restart)."""

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


def _sent_chunk_lists(bot) -> list[list[str]]:
    return [call.args[1] for call in bot.command_manager.send_channel_messages_chunked.call_args_list]


class _DummySource(EventAlertSourceBase):
    """Header-only formatter (like IMGW's -- description not merged in, so
    send_details triggers a genuine follow-up send)."""

    name = "dummy"

    def is_enabled(self) -> bool:
        return True

    def poll_interval(self) -> float:
        return 300

    async def check(self) -> None:
        pass

    def _format(self, record: dict) -> str:
        return f"⚠️ {record.get('title')}"


class _DummyMergedSource(_DummySource):
    """Formatter that merges the description into the primary text (like RSO's) --
    send_details must not duplicate it as a follow-up."""

    name = "dummy_merged"

    def _format(self, record: dict) -> str:
        text = f"📢 {record.get('title')}"
        description = record.get("description")
        if description:
            text = f"{text} — {description}"
        return text


def _source(bot, cls=_DummySource):
    service = AlertsService(bot)
    return cls(service)


@pytest.mark.unit
class TestDedup:
    async def test_new_alert_is_sent(self):
        bot = _bot()
        source = _source(bot)
        await source._process_new_alerts(
            [RECORD_A], source._format, send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        bot.command_manager.send_channel_messages_chunked.assert_awaited_once()

    async def test_same_alert_polled_twice_sent_once(self):
        bot = _bot()
        source = _source(bot)
        for _ in range(2):
            await source._process_new_alerts(
                [RECORD_A], source._format, send_details=False, max_alerts_per_hour=12, max_age_hours=24,
            )
        bot.command_manager.send_channel_messages_chunked.assert_awaited_once()

    async def test_no_new_alerts_sends_nothing(self):
        bot = _bot()
        source = _source(bot)
        await source._process_new_alerts(
            [], source._format, send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        bot.command_manager.send_channel_messages_chunked.assert_not_awaited()

    async def test_restart_safety_persisted_id_prevents_resend(self):
        """Simulates a restart: a fresh source sharing the same db_manager must
        not resend an alert whose id was already persisted."""
        db_manager = _FakeDBManager()
        db_manager.set_metadata("alerts_dummy_seen_ids", json.dumps([RECORD_A["external_id"]]))
        bot = _bot(db_manager=db_manager)
        source = _source(bot)  # loads seen ids from db_manager on construction

        await source._process_new_alerts(
            [RECORD_A], source._format, send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        bot.command_manager.send_channel_messages_chunked.assert_not_awaited()

    async def test_seen_ids_persisted_after_send(self):
        db_manager = _FakeDBManager()
        bot = _bot(db_manager=db_manager)
        source = _source(bot)
        await source._process_new_alerts(
            [RECORD_A], source._format, send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        persisted = json.loads(db_manager.get_metadata("alerts_dummy_seen_ids"))
        assert RECORD_A["external_id"] in persisted


@pytest.mark.unit
class TestRateLimit:
    async def test_alerts_beyond_cap_are_suppressed_not_sent(self):
        bot = _bot()
        source = _source(bot)
        record_2 = dict(RECORD_A, external_id="Wr_second", published_at=_recent(-1.5))
        await source._process_new_alerts(
            [RECORD_A, record_2], source._format, send_details=False, max_alerts_per_hour=1, max_age_hours=24,
        )
        assert bot.command_manager.send_channel_messages_chunked.await_count == 1

    async def test_suppressed_alert_still_marked_seen(self):
        """A suppressed alert is a hard drop (logged), not a deferred retry."""
        bot = _bot()
        source = _source(bot)
        record_2 = dict(RECORD_A, external_id="Wr_second", published_at=_recent(-1.5))
        await source._process_new_alerts(
            [RECORD_A, record_2], source._format, send_details=False, max_alerts_per_hour=1, max_age_hours=24,
        )
        bot.command_manager.send_channel_messages_chunked.reset_mock()
        await source._process_new_alerts(
            [RECORD_A, record_2], source._format, send_details=False, max_alerts_per_hour=1, max_age_hours=24,
        )
        bot.command_manager.send_channel_messages_chunked.assert_not_awaited()


@pytest.mark.unit
class TestSendFailureNotLost:
    """Regression coverage for a real bug caught in production: a burst of new
    alerts collided with the bot's own global TX rate limiter, and the old code
    counted/marked every one of them as sent regardless. A failed send must not
    be marked seen, so it is retried.
    """

    async def test_failed_send_is_not_marked_seen(self):
        bot = _bot()
        bot.command_manager.send_channel_messages_chunked = AsyncMock(return_value=False)
        source = _source(bot)
        await source._process_new_alerts(
            [RECORD_A], source._format, send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        assert RECORD_A["external_id"] not in source._seen_ids

    async def test_failed_send_is_retried_on_next_poll(self):
        bot = _bot()
        # Fails the first time, succeeds the second (simulates the limiter
        # clearing, or a transient error resolving, by the next poll).
        bot.command_manager.send_channel_messages_chunked = AsyncMock(side_effect=[False, True])
        source = _source(bot)

        await source._process_new_alerts(
            [RECORD_A], source._format, send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        await source._process_new_alerts(
            [RECORD_A], source._format, send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        assert bot.command_manager.send_channel_messages_chunked.await_count == 2
        assert RECORD_A["external_id"] in source._seen_ids

    async def test_send_details_followup_skipped_when_primary_send_fails(self):
        bot = _bot()
        bot.command_manager.send_channel_messages_chunked = AsyncMock(return_value=False)
        source = _source(bot)
        await source._process_new_alerts(
            [RECORD_A], source._format, send_details=True, max_alerts_per_hour=12, max_age_hours=24,
        )
        # Only the one (failed) primary-message attempt -- no follow-up detail send.
        assert bot.command_manager.send_channel_messages_chunked.await_count == 1


@pytest.mark.unit
class TestMessageChunking:
    """Long alert text is split into several messages instead of being truncated
    with '...'."""

    async def test_short_alert_is_a_single_chunk(self):
        bot = _bot()
        source = _source(bot)
        await source._process_new_alerts(
            [RECORD_A], source._format, send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        chunk_lists = _sent_chunk_lists(bot)
        assert len(chunk_lists) == 1
        assert len(chunk_lists[0]) == 1  # one message, not split

    async def test_long_description_splits_into_multiple_messages_not_truncated(self):
        bot = _bot()
        source = _source(bot, cls=_DummyMergedSource)
        long_description = (
            "Prognozowane sa intensywne opady deszczu oraz silny wiatr w wielu "
            "powiatach wojewodztwa podlaskiego. Mieszkancy powinni zabezpieczyc "
            "mienie, unikac przebywania w lasach i na otwartej przestrzeni, oraz "
            "sledzic komunikaty sluzb ratowniczych. Mozliwe sa lokalne podtopienia "
            "oraz przerwy w dostawie pradu elektrycznego na terenach nizinnych."
        )
        record = dict(RECORD_A, description=long_description)
        await source._process_new_alerts(
            [record], source._format, send_details=False, max_alerts_per_hour=12, max_age_hours=24,
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
        bot = _bot()
        source = _source(bot, cls=_DummyMergedSource)
        record = dict(RECORD_A, description="słowo " * 60)
        await source._process_new_alerts(
            [record], source._format, send_details=False, max_alerts_per_hour=12, max_age_hours=24,
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
        bot = _bot()
        source = _source(bot)
        record = dict(RECORD_A, published_at=self._timestamp(hours_ago=240))  # 10 days
        assert source._is_stale(record, max_age_hours=24) is True

    def test_is_stale_false_for_recent_timestamp(self):
        bot = _bot()
        source = _source(bot)
        record = dict(RECORD_A, published_at=self._timestamp(hours_ago=1))
        assert source._is_stale(record, max_age_hours=24) is False

    def test_is_stale_fails_open_when_no_timestamp(self):
        """No published_at or valid_from at all -- don't suppress on a guess."""
        bot = _bot()
        source = _source(bot)
        record = dict(RECORD_A, published_at=None, valid_from=None)
        assert source._is_stale(record, max_age_hours=24) is False

    def test_is_stale_falls_back_to_valid_from(self):
        bot = _bot()
        source = _source(bot)
        record = dict(RECORD_A, published_at=None, valid_from=self._timestamp(hours_ago=240))
        assert source._is_stale(record, max_age_hours=24) is True

    async def test_stale_alert_is_not_sent(self):
        bot = _bot()
        source = _source(bot)
        old_record = dict(RECORD_A, published_at=self._timestamp(hours_ago=240))
        await source._process_new_alerts(
            [old_record], source._format, send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        bot.command_manager.send_channel_messages_chunked.assert_not_awaited()

    async def test_stale_alert_is_marked_seen_so_its_not_rechecked_forever(self):
        bot = _bot()
        source = _source(bot)
        old_record = dict(RECORD_A, published_at=self._timestamp(hours_ago=240))
        await source._process_new_alerts(
            [old_record], source._format, send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        assert old_record["external_id"] in source._seen_ids

    async def test_fresh_alert_still_sent_alongside_a_stale_one(self):
        bot = _bot()
        source = _source(bot)
        old_record = dict(RECORD_A, external_id="old", published_at=self._timestamp(hours_ago=240))
        fresh_record = dict(RECORD_A, external_id="fresh", published_at=self._timestamp(hours_ago=1))
        await source._process_new_alerts(
            [old_record, fresh_record], source._format,
            send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        bot.command_manager.send_channel_messages_chunked.assert_awaited_once()
        assert "old" in source._seen_ids
        assert "fresh" in source._seen_ids


@pytest.mark.unit
class TestFloodScope:
    async def test_flood_scope_passed_to_send(self):
        bot = _bot(flood_scope="pl-podlasie")
        source = _source(bot)
        await source._process_new_alerts(
            [RECORD_A], source._format, send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        _, kwargs = bot.command_manager.send_channel_messages_chunked.call_args
        assert kwargs["scope"] == "pl-podlasie"

    async def test_empty_flood_scope_passes_none(self):
        bot = _bot()
        source = _source(bot)
        await source._process_new_alerts(
            [RECORD_A], source._format, send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        _, kwargs = bot.command_manager.send_channel_messages_chunked.call_args
        assert kwargs["scope"] is None


@pytest.mark.unit
class TestSendDetails:
    async def test_send_details_false_sends_one_call(self):
        bot = _bot()
        source = _source(bot)
        await source._process_new_alerts(
            [RECORD_A], source._format, send_details=False, max_alerts_per_hour=12, max_age_hours=24,
        )
        assert bot.command_manager.send_channel_messages_chunked.await_count == 1

    async def test_send_details_true_sends_followup_call_when_header_omits_description(self):
        bot = _bot()
        source = _source(bot)
        await source._process_new_alerts(
            [RECORD_A], source._format, send_details=True, max_alerts_per_hour=12, max_age_hours=24,
        )
        assert bot.command_manager.send_channel_messages_chunked.await_count == 2
        second_call_text = "".join(_sent_chunk_lists(bot)[1])
        assert second_call_text.startswith("Prognozowane")

    async def test_send_details_true_does_not_duplicate_when_formatter_already_merged_it(self):
        bot = _bot()
        source = _source(bot, cls=_DummyMergedSource)
        await source._process_new_alerts(
            [RECORD_A], source._format, send_details=True, max_alerts_per_hour=12, max_age_hours=24,
        )
        assert bot.command_manager.send_channel_messages_chunked.await_count == 1
