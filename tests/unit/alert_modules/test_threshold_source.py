"""Unit tests for ThresholdAlertSourceBase: hysteresis state machine (alert on
crossing up, stay silent while still elevated until the renotify interval, alert
once on crossing back down) and its bot_metadata persistence.

GIOS is the only current consumer, but the state machine itself (_apply_threshold_state)
is generic and exercised here via a minimal concrete dummy source; GIOS-specific
behavior (category ranking, fetch integration) belongs in test_gios.py once
gios.py exists.
"""

import configparser
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from modules.command_manager import CommandManager
from modules.i18n import Translator
from modules.service_plugins.alert_modules.threshold_source import ThresholdAlertSourceBase
from modules.service_plugins.alerts_service import AlertsService


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


def _sent_chunk_lists(bot) -> list[list[str]]:
    return [call.args[1] for call in bot.command_manager.send_channel_messages_chunked.call_args_list]


class _DummyThresholdSource(ThresholdAlertSourceBase):
    name = "dummy"

    def is_enabled(self) -> bool:
        return True

    def poll_interval(self) -> float:
        return 300

    async def check(self) -> None:
        pass

    async def probe(self, station_key: str, elevated: bool, renotify_hours: float = 6.0) -> bool:
        """Runs one threshold check + send, like a real source's check() would --
        returns True iff something was actually sent."""
        text = await self._apply_threshold_state(
            station_key=station_key,
            elevated=elevated,
            build_alert_text=lambda: "ALERT",
            build_normal_text=lambda: "NORMAL",
            renotify_hours=renotify_hours,
        )
        if text:
            await self._send_simple_message(text)
            self._persist_threshold_state()
        return text is not None


def _source(bot):
    service = AlertsService(bot)
    return _DummyThresholdSource(service)


@pytest.mark.unit
class TestThresholdHysteresis:
    async def test_no_alert_when_below_threshold(self):
        bot = _bot()
        source = _source(bot)
        await source.probe("station-1", elevated=False)
        bot.command_manager.send_channel_messages_chunked.assert_not_awaited()

    async def test_alert_when_crossing_into_elevated(self):
        bot = _bot()
        source = _source(bot)
        sent = await source.probe("station-1", elevated=True)
        assert sent is True
        bot.command_manager.send_channel_messages_chunked.assert_awaited_once()
        assert "".join(_sent_chunk_lists(bot)[0]) == "ALERT"

    async def test_no_repeat_alert_before_renotify_interval(self):
        bot = _bot()
        source = _source(bot)
        await source.probe("station-1", elevated=True, renotify_hours=6)  # crosses -> alerts
        bot.command_manager.send_channel_messages_chunked.reset_mock()
        await source.probe("station-1", elevated=True, renotify_hours=6)  # still elevated, not due
        bot.command_manager.send_channel_messages_chunked.assert_not_awaited()

    async def test_repeat_alert_after_renotify_interval_elapses(self):
        bot = _bot()
        source = _source(bot)
        await source.probe("station-1", elevated=True, renotify_hours=6)
        # Backdate the last notification past the renotify window.
        source._state["station-1"]["last_notified_at"] = (
            datetime.now() - timedelta(hours=7)
        ).isoformat()
        bot.command_manager.send_channel_messages_chunked.reset_mock()
        await source.probe("station-1", elevated=True, renotify_hours=6)
        bot.command_manager.send_channel_messages_chunked.assert_awaited_once()

    async def test_normal_message_on_recovery(self):
        bot = _bot()
        source = _source(bot)
        source._state["station-1"] = {"state": "elevated", "last_notified_at": None}
        sent = await source.probe("station-1", elevated=False)
        assert sent is True
        assert source._state["station-1"]["state"] == "normal"
        assert "".join(_sent_chunk_lists(bot)[0]) == "NORMAL"


@pytest.mark.unit
class TestThresholdStatePersistence:
    async def test_state_persisted_after_transition(self):
        db_manager = _FakeDBManager()
        bot = _bot(db_manager=db_manager)
        source = _source(bot)
        await source.probe("station-1", elevated=True)
        persisted = json.loads(db_manager.get_metadata("alerts_dummy_threshold_state"))
        assert persisted["station-1"]["state"] == "elevated"

    async def test_state_survives_restart(self):
        """A fresh source sharing the same db_manager must not re-alert for a
        station that's already in the elevated state."""
        db_manager = _FakeDBManager()
        bot = _bot(db_manager=db_manager)
        source = _source(bot)
        await source.probe("station-1", elevated=True)

        bot2 = _bot(db_manager=db_manager)
        source2 = _source(bot2)
        assert source2._state["station-1"]["state"] == "elevated"
        await source2.probe("station-1", elevated=True)
        bot2.command_manager.send_channel_messages_chunked.assert_not_awaited()
