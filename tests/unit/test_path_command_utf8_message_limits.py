#!/usr/bin/env python3
"""Unit tests for PathCommand UTF-8 byte truncation and multi-message splitting (PR #128)."""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from modules.commands.path_command import PathCommand
from modules.models import MeshMessage


@pytest.mark.unit
class TestPathCommandTruncateToByteLength:
    """Tests for _truncate_to_byte_length (no split within code points)."""

    @pytest.fixture
    def path_cmd(self, mock_bot):
        return PathCommand(mock_bot)

    def test_short_string_unchanged(self, path_cmd):
        assert path_cmd._truncate_to_byte_length("hello", 20) == "hello"

    def test_truncates_on_utf8_bytes_not_chars(self, path_cmd):
        """Budget is UTF-8 bytes; result must respect max_bytes including ellipsis."""
        ellipsis = "..."
        out = path_cmd._truncate_to_byte_length("😀😀", 7, ellipsis)
        assert out.endswith(ellipsis)
        assert len(out.encode("utf-8")) <= 7

    def test_does_not_emit_lone_surrogate_fragment(self, path_cmd):
        """Truncated bytes decode with errors='ignore' — result must be valid UTF-8."""
        out = path_cmd._truncate_to_byte_length("éééé", 5, "...")
        assert out.encode("utf-8") == out.encode("utf-8")  # round-trip
        out.encode("utf-8").decode("utf-8")  # no exception


@pytest.mark.unit
class TestPathCommandFormatPathResponseByteCap:
    """_format_path_response applies per-line UTF-8 byte cap (150)."""

    @pytest.fixture
    def path_cmd(self, mock_bot):
        cmd = PathCommand(mock_bot)
        cmd.translate = MockTranslate()
        return cmd

    def test_unknown_line_with_emoji_truncated_to_byte_budget(self, path_cmd):
        """Line may be few characters but many bytes; must fit 150-byte line cap."""
        node_ids = ["AB"]
        repeater_info = {"AB": {"found": False}}
        raw = path_cmd._format_path_response(node_ids, repeater_info)
        assert len(raw.encode("utf-8")) <= 150
        assert "AB" in raw


class MockTranslate:
    """Minimal translate: long unknown line to exercise 150-byte line cap."""

    def __call__(self, key: str, **kwargs):
        if key == "commands.path.node_unknown":
            node_id = kwargs.get("node_id", "")
            return f"unknown {node_id}" + "😀" * 50
        if key == "commands.path.truncation":
            return "..."
        return key


@pytest.mark.unit
class TestPathCommandSendPathResponseByteSplitting:
    """_send_path_response splits on UTF-8 byte length, not character count."""

    @pytest.fixture
    def path_cmd(self, mock_bot):
        cmd = PathCommand(mock_bot)
        cmd.translate = MockTranslateForSend()
        cmd.send_response = AsyncMock(return_value=True)
        return cmd

    @pytest.mark.asyncio
    async def test_splits_when_combined_lines_exceed_byte_budget(self, path_cmd):
        """Two lines: 12 + newline + 13 = 26 UTF-8 bytes > budget 25 -> second send."""
        path_cmd.get_max_message_length = lambda _msg: 25
        msg = MeshMessage(content="path", channel="general", is_dm=False)
        response = "a" * 12 + "\n" + "b" * 13
        with patch("modules.commands.path_command.asyncio.sleep", new_callable=AsyncMock):
            await path_cmd._send_path_response(msg, response)
        assert path_cmd.send_response.await_count >= 2

    @pytest.mark.asyncio
    async def test_single_send_when_under_byte_budget(self, path_cmd):
        path_cmd.get_max_message_length = lambda _msg: 100
        msg = MeshMessage(content="path", channel="general", is_dm=False)
        response = "short"
        with patch("modules.commands.path_command.asyncio.sleep", new_callable=AsyncMock):
            await path_cmd._send_path_response(msg, response)
        path_cmd.send_response.assert_awaited_once()


@pytest.mark.unit
class TestPathCommandReplyPrefix:
    """reply_prefix prepended to first RF payload only; last_response includes prefix."""

    @pytest.fixture
    def path_cmd(self, mock_bot):
        mock_bot.translator = MagicMock()
        mock_bot.translator.translate = Mock(side_effect=lambda key, **kwargs: key)
        cmd = PathCommand(mock_bot)
        cmd.send_response = AsyncMock(return_value=True)
        return cmd

    @pytest.mark.asyncio
    async def test_prepends_prefix_to_single_send(self, path_cmd, mock_bot):
        path_cmd.path_reply_prefix = "[{sender}]"
        path_cmd.get_max_message_length = lambda _msg: 200
        msg = MeshMessage(content="path", channel="general", is_dm=False, sender_id="alice")
        await path_cmd._send_path_response(msg, "line1")
        path_cmd.send_response.assert_awaited_once()
        payload = path_cmd.send_response.call_args[0][1]
        assert payload == "[alice]\nline1"
        assert path_cmd.last_response == "[alice]\nline1"

    @pytest.mark.asyncio
    async def test_prefix_supports_hops_and_enhanced_path(self, path_cmd, mock_bot):
        path_cmd.path_reply_prefix = "[{sender}] {path} ({hops_label})"
        path_cmd.get_max_message_length = lambda _msg: 200
        msg = MeshMessage(
            content="path",
            channel="general",
            is_dm=False,
            sender_id="alice",
            hops=2,
            path="raw fallback",
            routing_info={"path_length": 2, "path_nodes": ["01", "5f"]},
        )

        await path_cmd._send_path_response(msg, "line1")

        path_cmd.send_response.assert_awaited_once()
        payload = path_cmd.send_response.call_args[0][1]
        assert payload == "[alice] 01,5f (2 hops) (2 hops)\nline1"
        assert path_cmd.last_response == "[alice] 01,5f (2 hops) (2 hops)\nline1"

    @pytest.mark.asyncio
    async def test_prefix_only_on_first_split_message(self, path_cmd, mock_bot):
        path_cmd.path_reply_prefix = "P:"
        path_cmd.get_max_message_length = lambda _msg: 25
        path_cmd.translate = MockTranslateForSend()
        msg = MeshMessage(content="path", channel="general", is_dm=False)
        response = "a" * 12 + "\n" + "b" * 13
        with patch("modules.commands.path_command.asyncio.sleep", new_callable=AsyncMock):
            await path_cmd._send_path_response(msg, response)
        assert path_cmd.send_response.await_count >= 2
        first = path_cmd.send_response.call_args_list[0][0][1]
        assert first.startswith("P:\n")
        second = path_cmd.send_response.call_args_list[1][0][1]
        assert not second.startswith("P:\n")

    @pytest.mark.asyncio
    async def test_no_reply_prefix_falls_back_to_inline_sender_tag(self, path_cmd, mock_bot):
        """Lost once already during an upstream sync (see CLAUDE.md) - regression guard.

        With no Path_Command.reply_prefix configured, Path must fall back to a
        "@[sender]" tag on its own line above the response.
        """
        path_cmd.path_reply_prefix = ""
        path_cmd.get_max_message_length = lambda _msg: 200
        msg = MeshMessage(content="path", channel="general", is_dm=False, sender_id="alice")
        await path_cmd._send_path_response(msg, "line1")
        path_cmd.send_response.assert_awaited_once()
        payload = path_cmd.send_response.call_args[0][1]
        assert payload == "@[alice]\nline1"
        assert path_cmd.last_response == "@[alice]\nline1"

    @pytest.mark.asyncio
    async def test_configured_reply_prefix_overrides_sender_fallback(self, path_cmd, mock_bot):
        """An explicit Path_Command.reply_prefix wins outright - no stacked fallback tag."""
        path_cmd.path_reply_prefix = "[{sender}]"
        path_cmd.get_max_message_length = lambda _msg: 200
        msg = MeshMessage(content="path", channel="general", is_dm=False, sender_id="alice")
        await path_cmd._send_path_response(msg, "line1")
        payload = path_cmd.send_response.call_args[0][1]
        assert payload == "[alice]\nline1"
        assert "@[alice]" not in payload

    @pytest.mark.asyncio
    async def test_phrase_appends_to_inline_sender_tag(self, path_cmd, mock_bot):
        """Restored from orphaned narcolo PR #14 (ea19e72), lost in the same
        upstream sync as the sender-tag fallback above.

        A text phrase (not valid path hex) must surface next to the sender
        tag, the same way test command's phrase_part does.
        """
        path_cmd.path_reply_prefix = ""
        path_cmd.get_max_message_length = lambda _msg: 200
        msg = MeshMessage(content="path hi", channel="general", is_dm=False, sender_id="alice")
        await path_cmd._send_path_response(msg, "line1", phrase="hi")
        payload = path_cmd.send_response.call_args[0][1]
        assert payload == "@[alice]: hi\nline1"

    @pytest.mark.asyncio
    async def test_phrase_appends_after_configured_reply_prefix(self, path_cmd, mock_bot):
        path_cmd.path_reply_prefix = "[{sender}]"
        path_cmd.get_max_message_length = lambda _msg: 200
        msg = MeshMessage(content="path hi", channel="general", is_dm=False, sender_id="alice")
        await path_cmd._send_path_response(msg, "line1", phrase="hi")
        payload = path_cmd.send_response.call_args[0][1]
        assert payload == "[alice]\nhi\nline1"


@pytest.mark.unit
class TestPathCommandIsHexPath:
    """_is_hex_path distinguishes hex path data from a text phrase (ea19e72)."""

    @pytest.fixture
    def path_cmd(self, mock_bot):
        return PathCommand(mock_bot)

    @pytest.mark.parametrize("text", [
        "01,5f,a4", "01 5f a4", "015fa4", "01,5f (2 hops)", "a449cd",
    ])
    def test_recognizes_hex_forms(self, path_cmd, text):
        assert path_cmd._is_hex_path(text) is True

    @pytest.mark.parametrize("text", [
        "hi", "hello there", "", "123",  # odd-length string is not valid hex path
    ])
    def test_rejects_text_phrases(self, path_cmd, text):
        assert path_cmd._is_hex_path(text) is False

    @pytest.mark.asyncio
    async def test_execute_treats_hex_arg_as_path_decode(self, path_cmd, mock_bot):
        path_cmd._decode_path = AsyncMock(return_value="decoded")
        path_cmd._extract_path_from_recent_messages = AsyncMock(return_value="extracted")
        path_cmd._send_path_response = AsyncMock()
        path_cmd.enforce_path_byte_requirement = AsyncMock(return_value=True)
        msg = MeshMessage(content="path 01,5f", channel="general", is_dm=False, sender_id="alice")
        await path_cmd.execute(msg)
        path_cmd._decode_path.assert_awaited_once_with("01,5f", message=msg)
        path_cmd._send_path_response.assert_awaited_once_with(msg, "decoded", "")

    @pytest.mark.asyncio
    async def test_execute_treats_text_arg_as_phrase(self, path_cmd, mock_bot):
        path_cmd._decode_path = AsyncMock(return_value="decoded")
        path_cmd._extract_path_from_recent_messages = AsyncMock(return_value="extracted")
        path_cmd._send_path_response = AsyncMock()
        path_cmd.enforce_path_byte_requirement = AsyncMock(return_value=True)
        msg = MeshMessage(content="path hi there", channel="general", is_dm=False, sender_id="alice")
        await path_cmd.execute(msg)
        path_cmd._decode_path.assert_not_awaited()
        path_cmd._extract_path_from_recent_messages.assert_awaited_once_with(msg)
        path_cmd._send_path_response.assert_awaited_once_with(msg, "extracted", "hi there")


class MockTranslateForSend:
    def __call__(self, key: str, **kwargs):
        if key == "commands.path.continuation_end":
            return "\n>>"
        if key == "commands.path.continuation_start":
            return f"<< {kwargs.get('line', '')}"
        return key
