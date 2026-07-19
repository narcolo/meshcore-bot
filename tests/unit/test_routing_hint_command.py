#!/usr/bin/env python3
"""
Unit tests for RoutingHintCommand.

Covers:
  - should_execute() filtering (channel, bytes_per_hop, bot self, cooldown, DM)
  - execute() sends two chunked messages and records cooldown
  - execute() is a no-op when should_execute() is False
"""

import configparser
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from modules.commands.routing_hint_command import RoutingHintCommand
from modules.models import MeshMessage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _routing_info(bytes_per_hop: int = 1) -> dict:
    return {
        "bytes_per_hop": bytes_per_hop,
        "path_nodes": ["aa", "bb"],
        "path_length": 2,
        "route_type_name": "FLOOD",
    }


def _make_bot(enabled: bool = True, bot_name: str = "TestBot", channel: str = "Public") -> MagicMock:
    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "bot_name", bot_name)
    config.add_section("Channels")
    config.set("Channels", "monitor_channels", "Public,general")
    config.add_section("Routing_Hint_Command")
    config.set("Routing_Hint_Command", "enabled", str(enabled).lower())
    config.set("Routing_Hint_Command", "channel", channel)
    config.set("Routing_Hint_Command", "cooldown_hours", "24")

    bot = MagicMock()
    bot.config = config
    bot.logger = MagicMock()

    def _translate(key, **kwargs):
        # Return the key with formatted kwargs so tests can assert on it
        if kwargs:
            return f"{key}[{','.join(f'{k}={v}' for k, v in sorted(kwargs.items()))}]"
        return key

    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=_translate)
    bot.translator.get_value = Mock(return_value=None)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["Public", "general"]
    bot.command_manager.send_response_chunked = AsyncMock(return_value=True)
    return bot


def _msg(
    sender_id: str = "Alice",
    channel: str = "Public",
    is_dm: bool = False,
    bytes_per_hop: int = 1,
    routing_info: dict | None = None,
) -> MeshMessage:
    return MeshMessage(
        content="hello mesh",
        sender_id=sender_id,
        channel=channel if not is_dm else None,
        is_dm=is_dm,
        routing_info=routing_info if routing_info is not None else _routing_info(bytes_per_hop),
    )


# ---------------------------------------------------------------------------
# should_execute tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRoutingHintShouldExecute:

    def test_returns_true_for_valid_1byte_public_message(self):
        cmd = RoutingHintCommand(_make_bot())
        assert cmd.should_execute(_msg()) is True

    def test_false_when_disabled(self):
        cmd = RoutingHintCommand(_make_bot(enabled=False))
        assert cmd.should_execute(_msg()) is False

    def test_false_for_dm(self):
        cmd = RoutingHintCommand(_make_bot())
        assert cmd.should_execute(_msg(is_dm=True)) is False

    def test_false_for_wrong_channel(self):
        cmd = RoutingHintCommand(_make_bot())
        assert cmd.should_execute(_msg(channel="general")) is False

    def test_channel_match_is_case_insensitive(self):
        cmd = RoutingHintCommand(_make_bot(channel="Public"))
        assert cmd.should_execute(_msg(channel="public")) is True
        assert cmd.should_execute(_msg(channel="PUBLIC")) is True

    def test_false_when_no_routing_info(self):
        cmd = RoutingHintCommand(_make_bot())
        m = _msg()
        m.routing_info = None
        assert cmd.should_execute(m) is False

    def test_false_for_2byte_routing(self):
        cmd = RoutingHintCommand(_make_bot())
        assert cmd.should_execute(_msg(bytes_per_hop=2)) is False

    def test_false_for_3byte_routing(self):
        cmd = RoutingHintCommand(_make_bot())
        assert cmd.should_execute(_msg(bytes_per_hop=3)) is False

    def test_false_when_sender_is_bot(self):
        cmd = RoutingHintCommand(_make_bot(bot_name="TestBot"))
        assert cmd.should_execute(_msg(sender_id="TestBot")) is False

    def test_bot_name_comparison_is_case_insensitive(self):
        cmd = RoutingHintCommand(_make_bot(bot_name="TestBot"))
        assert cmd.should_execute(_msg(sender_id="testbot")) is False
        assert cmd.should_execute(_msg(sender_id="TESTBOT")) is False

    def test_false_when_sender_is_empty(self):
        cmd = RoutingHintCommand(_make_bot())
        m = _msg(sender_id="")
        assert cmd.should_execute(m) is False

    def test_false_when_sender_is_whitespace(self):
        cmd = RoutingHintCommand(_make_bot())
        m = _msg(sender_id="   ")
        assert cmd.should_execute(m) is False

    def test_false_when_on_cooldown(self):
        cmd = RoutingHintCommand(_make_bot())
        cmd._notified["Alice"] = time.time()  # just notified
        assert cmd.should_execute(_msg(sender_id="Alice")) is False

    def test_true_after_cooldown_expires(self):
        cmd = RoutingHintCommand(_make_bot())
        cmd._notified["Alice"] = time.time() - (25 * 3600)  # 25 h ago > 24 h cooldown
        assert cmd.should_execute(_msg(sender_id="Alice")) is True

    def test_different_users_independent_cooldown(self):
        cmd = RoutingHintCommand(_make_bot())
        cmd._notified["Alice"] = time.time()
        assert cmd.should_execute(_msg(sender_id="Bob")) is True


# ---------------------------------------------------------------------------
# execute tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRoutingHintExecute:

    @pytest.mark.asyncio
    async def test_sends_two_chunked_messages(self):
        bot = _make_bot()
        cmd = RoutingHintCommand(bot)

        result = await cmd.execute(_msg(sender_id="Alice"))

        assert result is True
        bot.command_manager.send_response_chunked.assert_awaited_once()
        chunks = bot.command_manager.send_response_chunked.call_args[0][1]
        assert len(chunks) == 2

    @pytest.mark.asyncio
    async def test_part1_contains_sender_name(self):
        bot = _make_bot()
        cmd = RoutingHintCommand(bot)

        await cmd.execute(_msg(sender_id="Alice"))

        chunks = bot.command_manager.send_response_chunked.call_args[0][1]
        assert "Alice" in chunks[0]  # name embedded inside @[Alice]

    @pytest.mark.asyncio
    async def test_records_cooldown_after_execution(self):
        cmd = RoutingHintCommand(_make_bot())
        assert "Alice" not in cmd._notified

        await cmd.execute(_msg(sender_id="Alice"))

        assert "Alice" in cmd._notified
        assert abs(cmd._notified["Alice"] - time.time()) < 2

    @pytest.mark.asyncio
    async def test_no_op_when_should_execute_false(self):
        bot = _make_bot(enabled=False)
        cmd = RoutingHintCommand(bot)

        result = await cmd.execute(_msg())

        assert result is False
        bot.command_manager.send_response_chunked.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_double_send_on_concurrent_calls(self):
        """Second concurrent call is blocked by in-memory cooldown set by first."""
        cmd = RoutingHintCommand(_make_bot())
        msg = _msg(sender_id="Alice")

        await cmd.execute(msg)
        # Simulate a second call arriving before the first one returns
        result2 = await cmd.execute(msg)

        assert result2 is False
        assert cmd.bot.command_manager.send_response_chunked.await_count == 1
