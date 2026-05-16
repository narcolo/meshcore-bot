#!/usr/bin/env python3
"""Unit tests for MentionCommand — Bender-style name-only trigger."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from modules.commands.mention_command import MentionCommand
from modules.models import MeshMessage


def make_message(content, is_dm=False, channel="#bot"):
    msg = MagicMock(spec=MeshMessage)
    msg.content = content
    msg.is_dm = is_dm
    msg.channel = channel if not is_dm else None
    msg.sender_id = "TEST"
    return msg


@pytest.fixture
def mention_cmd(mock_bot):
    mock_bot.config.get = MagicMock(return_value="Bender")
    mock_bot.meshcore = MagicMock()
    mock_bot.meshcore.self_info = None
    return MentionCommand(mock_bot)


@pytest.fixture
def mention_cmd_emoji(mock_bot):
    """Bot name with emoji suffix, matching real-world config: bot_name = Bender 🤖"""
    mock_bot.config.get = MagicMock(return_value="Bender 🤖")
    mock_bot.meshcore = MagicMock()
    mock_bot.meshcore.self_info = None
    return MentionCommand(mock_bot)


@pytest.mark.unit
class TestMentionCommandChannelTrigger:

    def test_bare_mention_triggers(self, mention_cmd):
        assert mention_cmd.matches_custom_syntax(make_message("@[Bender]")) is True

    def test_mention_with_spaces_triggers(self, mention_cmd):
        assert mention_cmd.matches_custom_syntax(make_message("@[Bender]   ")) is True

    def test_mention_with_text_does_not_trigger(self, mention_cmd):
        assert mention_cmd.matches_custom_syntax(make_message("@[Bender] hello")) is False

    def test_other_name_does_not_trigger(self, mention_cmd):
        assert mention_cmd.matches_custom_syntax(make_message("@[Alice]")) is False

    def test_two_mentions_does_not_trigger(self, mention_cmd):
        assert mention_cmd.matches_custom_syntax(make_message("@[Bender] @[Alice]")) is False

    def test_unrelated_text_does_not_trigger(self, mention_cmd):
        assert mention_cmd.matches_custom_syntax(make_message("hello world")) is False

    def test_case_insensitive_mention(self, mention_cmd):
        assert mention_cmd.matches_custom_syntax(make_message("@[bender]")) is True

    def test_empty_content_triggers(self, mention_cmd):
        """Simulates message_handler stripping @[BotName] before command sees it."""
        assert mention_cmd.matches_custom_syntax(make_message("")) is True

    def test_emoji_bot_name_mention_triggers(self, mention_cmd_emoji):
        """@[Bender] should match bot named 'Bender 🤖' (emoji stripped for comparison)."""
        assert mention_cmd_emoji.matches_custom_syntax(make_message("@[Bender]")) is True

    def test_emoji_bot_name_full_mention_triggers(self, mention_cmd_emoji):
        assert mention_cmd_emoji.matches_custom_syntax(make_message("@[Bender 🤖]")) is True

    def test_emoji_bot_name_with_text_does_not_trigger(self, mention_cmd_emoji):
        assert mention_cmd_emoji.matches_custom_syntax(make_message("@[Bender] hello")) is False


@pytest.mark.unit
class TestMentionCommandDMTrigger:

    def test_at_mention_in_dm_triggers(self, mention_cmd):
        assert mention_cmd.matches_custom_syntax(make_message("@[Bender]", is_dm=True)) is True

    def test_plain_name_in_dm_triggers(self, mention_cmd):
        assert mention_cmd.matches_custom_syntax(make_message("Bender", is_dm=True)) is True

    def test_plain_name_lowercase_in_dm_triggers(self, mention_cmd):
        assert mention_cmd.matches_custom_syntax(make_message("bender", is_dm=True)) is True

    def test_name_with_text_in_dm_does_not_trigger(self, mention_cmd):
        assert mention_cmd.matches_custom_syntax(make_message("Bender help", is_dm=True)) is False

    def test_empty_dm_does_not_trigger(self, mention_cmd):
        assert mention_cmd.matches_custom_syntax(make_message("", is_dm=True)) is False

    def test_unrelated_text_in_dm_does_not_trigger(self, mention_cmd):
        assert mention_cmd.matches_custom_syntax(make_message("hello", is_dm=True)) is False

    def test_emoji_bot_name_plain_in_dm_triggers(self, mention_cmd_emoji):
        """'Bender' in DM should match bot named 'Bender 🤖'."""
        assert mention_cmd_emoji.matches_custom_syntax(make_message("Bender", is_dm=True)) is True

    def test_emoji_bot_name_mention_in_dm_triggers(self, mention_cmd_emoji):
        assert mention_cmd_emoji.matches_custom_syntax(make_message("@[Bender]", is_dm=True)) is True


@pytest.mark.unit
class TestMentionCommandExecute:

    @pytest.mark.asyncio
    async def test_execute_sends_response(self, mention_cmd):
        mention_cmd.send_response = AsyncMock(return_value=True)
        msg = make_message("@[Bender]")
        result = await mention_cmd.execute(msg)
        assert result is True
        mention_cmd.send_response.assert_called_once()
        response_text = mention_cmd.send_response.call_args[0][1]
        assert isinstance(response_text, str)
        assert len(response_text) > 0

    @pytest.mark.asyncio
    async def test_execute_uses_translations_when_available(self, mention_cmd):
        mention_cmd.send_response = AsyncMock(return_value=True)
        mention_cmd.translate_get_value = MagicMock(return_value=["Testowa odpowiedź!"])
        msg = make_message("@[Bender]")
        await mention_cmd.execute(msg)
        response_text = mention_cmd.send_response.call_args[0][1]
        assert response_text == "Testowa odpowiedź!"

    @pytest.mark.asyncio
    async def test_execute_falls_back_when_no_translations(self, mention_cmd):
        mention_cmd.send_response = AsyncMock(return_value=True)
        mention_cmd.translate_get_value = MagicMock(return_value=None)
        msg = make_message("@[Bender]")
        await mention_cmd.execute(msg)
        response_text = mention_cmd.send_response.call_args[0][1]
        assert response_text in MentionCommand.FALLBACK_RESPONSES_PL
