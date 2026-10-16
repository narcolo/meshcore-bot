#!/usr/bin/env python3
"""Unit tests for BajojajoCommand — automatic Bender reaction to 'bajo jajo'."""

import random

import pytest
from unittest.mock import AsyncMock, MagicMock

from modules.commands.bajojajo_command import BajojajoCommand
from modules.commands.hello_command import HelloCommand
from modules.models import MeshMessage


def make_message(content, is_dm=False, channel="general", sender_id="TEST"):
    msg = MagicMock(spec=MeshMessage)
    msg.content = content
    msg.is_dm = is_dm
    msg.channel = channel if not is_dm else None
    msg.sender_id = sender_id
    return msg


@pytest.fixture
def bajojajo_cmd(command_mock_bot):
    # No hello command loaded by default -> falls back to translate_get_value,
    # which command_mock_bot's translator.get_value already returns None for,
    # so the minimal built-in address pool is exercised deterministically.
    command_mock_bot.command_manager.commands = {}
    return BajojajoCommand(command_mock_bot)


@pytest.fixture
def bajojajo_cmd_with_hello(command_mock_bot):
    """Same command, but with a real HelloCommand loaded, as in production."""
    command_mock_bot.config.add_section('Hello_Command')
    command_mock_bot.config.set('Hello_Command', 'enabled', 'true')
    hello_cmd = HelloCommand(command_mock_bot)
    command_mock_bot.command_manager.commands = {'hello': hello_cmd}
    return BajojajoCommand(command_mock_bot)


@pytest.mark.unit
class TestBajojajoTriggerMatching:
    """Covers spec items 1-5: trigger detection anywhere in the message body."""

    def test_matches_bajojajo_no_space(self, bajojajo_cmd):
        assert bajojajo_cmd.matches_custom_syntax(make_message("Bajojajo")) is True

    def test_matches_bajo_jajo_with_space(self, bajojajo_cmd):
        assert bajojajo_cmd.matches_custom_syntax(make_message("Bajo Jajo")) is True

    @pytest.mark.parametrize("content", ["bajojajo", "BAJOJAJO", "BaJoJaJo", "bajo jajo", "BAJO JAJO"])
    def test_case_insensitive(self, bajojajo_cmd, content):
        assert bajojajo_cmd.matches_custom_syntax(make_message(content)) is True

    @pytest.mark.parametrize("content", [
        "Co tam, bajo jajo?",
        "No i znowu \"bajojajo\"...",
        "bajojajo!",
        "(bajo jajo)",
    ])
    def test_matches_inside_longer_text_and_with_punctuation(self, bajojajo_cmd, content):
        assert bajojajo_cmd.matches_custom_syntax(make_message(content)) is True

    def test_does_not_match_unrelated_text(self, bajojajo_cmd):
        assert bajojajo_cmd.matches_custom_syntax(make_message("Dzień dobry wszystkim")) is False

    def test_does_not_match_inside_longer_word(self, bajojajo_cmd):
        """'bajojajko' contains no exact 'bajo...jajo' substring and must not match."""
        assert bajojajo_cmd.matches_custom_syntax(make_message("superbajojajko123")) is False

    def test_empty_content_does_not_match(self, bajojajo_cmd):
        assert bajojajo_cmd.matches_custom_syntax(make_message("")) is False

    @pytest.mark.parametrize("content", [
        "Bajojajobajojajo",
        "hello Bajojajobajojajo",
        "BAJOJAJOBAJOJAJO!",
    ])
    def test_matches_stuttered_repeat_with_no_separator(self, bajojajo_cmd, content):
        """Two trigger repeats glued together with no space/punctuation between
        them still match: TRIGGER_PATTERN has no boundary assertions, so
        "bajo"+"jajo" is found as soon as it occurs, regardless of what comes
        right before/after the match in the surrounding text.
        """
        assert bajojajo_cmd.matches_custom_syntax(make_message(content)) is True

    @pytest.mark.parametrize("content", [
        "bajo_jajo",
        "bajo__jajo",
        "bajo-jajo",
        "bajo.jajo",
        "bajo...jajo",
        "bajo  jajo",
        "bajo,jajo",
        "bajo-_.jajo",
    ])
    def test_matches_with_various_non_word_separators(self, bajojajo_cmd, content):
        """'bajo' then 'jajo' with any run of spaces/punctuation/underscores
        between them must match -- not just a single plain space.
        """
        assert bajojajo_cmd.matches_custom_syntax(make_message(content)) is True

    @pytest.mark.parametrize("content", [
        "jajobajo",
        "jajo bajo",
        "JAJO BAJO",
        "jajo-bajo",
    ])
    def test_reversed_word_order_does_not_match(self, bajojajo_cmd, content):
        """'jajo' before 'bajo' is the wrong order and must never trigger."""
        assert bajojajo_cmd.matches_custom_syntax(make_message(content)) is False

    @pytest.mark.parametrize("content", [
        "bajo tam jajo",
        "bajo123jajo",
        "bajoXjajo",
        "bajo i jajo",
    ])
    def test_unrelated_word_between_bajo_and_jajo_does_not_match(self, bajojajo_cmd, content):
        """Only non-word/underscore separators may sit between 'bajo' and
        'jajo' -- an actual letter/digit/word in between must not match.
        """
        assert bajojajo_cmd.matches_custom_syntax(make_message(content)) is False


@pytest.mark.unit
class TestBajojajoSingleReplyPerMessage:
    """Covers spec items 6-7: multiple occurrences/spellings -> exactly one reply."""

    def test_multiple_occurrences_still_a_single_boolean_match(self, bajojajo_cmd):
        msg = make_message("Bajojajo! Bajo jajo! BAJOJAJO!")
        assert bajojajo_cmd.matches_custom_syntax(msg) is True

    def test_both_spellings_in_one_message_still_single_match(self, bajojajo_cmd):
        msg = make_message("bajojajo i bajo jajo w jednej wiadomości")
        assert bajojajo_cmd.matches_custom_syntax(msg) is True

    @pytest.mark.asyncio
    async def test_execute_sends_exactly_one_response_for_multiple_occurrences(self, bajojajo_cmd):
        bajojajo_cmd.send_response = AsyncMock(return_value=True)
        msg = make_message("Bajojajo! Bajo jajo! BAJOJAJO!")
        assert bajojajo_cmd.matches_custom_syntax(msg) is True
        await bajojajo_cmd.execute(msg)
        bajojajo_cmd.send_response.assert_called_once()


@pytest.mark.unit
class TestBajojajoNicknameVsBody:
    """Covers spec item 8: a nickname containing the keyword must not trigger by itself."""

    def test_nickname_containing_keyword_with_clean_body_does_not_trigger(self, bajojajo_cmd):
        msg = make_message("Hello there, how are you?", sender_id="Bajojajo_Jan")
        assert bajojajo_cmd.matches_custom_syntax(msg) is False

    def test_nickname_containing_keyword_with_matching_body_still_triggers(self, bajojajo_cmd):
        msg = make_message("ok bajojajo then", sender_id="Bajojajo_Jan")
        assert bajojajo_cmd.matches_custom_syntax(msg) is True

    def test_mention_of_a_user_named_bajojajo_does_not_trigger(self, bajojajo_cmd):
        """A real user can be named 'Bajojajo'. Someone addressing them via the
        bot's @[username] mention syntax (e.g. replying to them) must not count
        as the meme keyword appearing in the message -- only literal text does.
        """
        msg = make_message("@[Bajojajo] hej, jak leci?")
        assert bajojajo_cmd.matches_custom_syntax(msg) is False

    def test_mention_of_user_named_bajojajo_plus_real_trigger_still_matches(self, bajojajo_cmd):
        msg = make_message("@[Bajojajo] no i bajojajo")
        assert bajojajo_cmd.matches_custom_syntax(msg) is True

    def test_matching_is_purely_content_based_not_sender_based(self, bajojajo_cmd):
        """Matching never consults sender_id -- any upstream self-message or
        retransmission filtering (bot's own outgoing messages are never
        redelivered as incoming events; duplicate/retransmitted packets are
        deduped before process_message runs) is handled generically for every
        command, the same way as for hello/thankyou. This command adds no
        sender-identity special-casing of its own.
        """
        content = "no bajojajo tutaj!"
        for sender in (None, "", "TestBot", "AnyUser", "Bajojajo_Jan"):
            msg = make_message(content, sender_id=sender)
            assert bajojajo_cmd.matches_custom_syntax(msg) is True


@pytest.mark.unit
class TestBajojajoReplyMechanism:
    """Covers spec item 9: replies go through the normal reply mechanism."""

    @pytest.mark.asyncio
    async def test_execute_replies_via_send_response_to_same_message(self, bajojajo_cmd):
        bajojajo_cmd.send_response = AsyncMock(return_value=True)
        msg = make_message("bajojajo")
        result = await bajojajo_cmd.execute(msg)
        assert result is True
        bajojajo_cmd.send_response.assert_called_once()
        called_message, called_content = bajojajo_cmd.send_response.call_args[0]
        assert called_message is msg
        assert isinstance(called_content, str) and len(called_content) > 0

    @pytest.mark.asyncio
    async def test_execute_works_for_dm_too(self, bajojajo_cmd):
        bajojajo_cmd.send_response = AsyncMock(return_value=True)
        msg = make_message("bajo jajo", is_dm=True)
        result = await bajojajo_cmd.execute(msg)
        assert result is True
        bajojajo_cmd.send_response.assert_called_once()


@pytest.mark.unit
class TestBajojajoSenderTag:
    """Covers spec item 5: the reply visibly tags the triggering sender in
    channels, using the same '@[name]' mention syntax as the rest of the bot.
    """

    @pytest.mark.asyncio
    async def test_channel_reply_is_tagged_with_sender(self, bajojajo_cmd):
        bajojajo_cmd.send_response = AsyncMock(return_value=True)
        msg = make_message("bajojajo", sender_id="Jajobajo")
        await bajojajo_cmd.execute(msg)
        response = bajojajo_cmd.send_response.call_args[0][1]
        assert response.startswith("@[Jajobajo] ")

    @pytest.mark.asyncio
    async def test_dm_reply_is_not_tagged(self, bajojajo_cmd):
        """DMs have only one other party -- tagging the sender would be noise."""
        bajojajo_cmd.send_response = AsyncMock(return_value=True)
        msg = make_message("bajojajo", is_dm=True, sender_id="Jajobajo")
        await bajojajo_cmd.execute(msg)
        response = bajojajo_cmd.send_response.call_args[0][1]
        assert "@[Jajobajo]" not in response

    @pytest.mark.asyncio
    async def test_sender_tag_disabled_via_config(self, command_mock_bot):
        command_mock_bot.command_manager.commands = {}
        command_mock_bot.config.add_section('Bajojajo_Command')
        command_mock_bot.config.set('Bajojajo_Command', 'include_sender', 'false')
        cmd = BajojajoCommand(command_mock_bot)
        cmd.send_response = AsyncMock(return_value=True)
        msg = make_message("bajojajo", sender_id="Jajobajo")
        await cmd.execute(msg)
        response = cmd.send_response.call_args[0][1]
        assert "@[Jajobajo]" not in response

    @pytest.mark.asyncio
    async def test_sender_name_with_brackets_is_sanitized(self, bajojajo_cmd):
        """A hostile sender name must not be able to forge a second mention or
        break the reply's own str.format() pass (mirrors HelloCommand's guard).
        """
        bajojajo_cmd.send_response = AsyncMock(return_value=True)
        msg = make_message("bajojajo", sender_id="Evil][Admin]{oops}")
        await bajojajo_cmd.execute(msg)
        response = bajojajo_cmd.send_response.call_args[0][1]
        # The tag is the first "@[...] " token; nothing inside it is a stray
        # bracket/brace that could forge a second mention or break formatting.
        tag_inner = response.split("] ", 1)[0].removeprefix("@[")
        assert "[" not in tag_inner and "]" not in tag_inner
        assert "{" not in tag_inner and "}" not in tag_inner

    @pytest.mark.asyncio
    async def test_unknown_channel_sender_is_not_tagged(self, bajojajo_cmd):
        msg = make_message("bajojajo", sender_id=HelloCommand.UNKNOWN_CHANNEL_SENDER)
        bajojajo_cmd.send_response = AsyncMock(return_value=True)
        await bajojajo_cmd.execute(msg)
        response = bajojajo_cmd.send_response.call_args[0][1]
        assert not response.startswith("@[")


@pytest.mark.unit
class TestBajojajoResponseContent:
    """Covers spec items 3/12 and the base+optional-suffix structure:
    the main Bender phrase always comes first, the CB-radio meme reference is
    an optional suffix that never leads and is never duplicated, and the
    address keeps reusing hello's randomized vocabulary.
    """

    @pytest.mark.asyncio
    async def test_response_starts_with_the_base_phrase(self, bajojajo_cmd):
        bajojajo_cmd.include_sender = False  # isolate from the sender-tag feature (its own test class below)
        bajojajo_cmd.send_response = AsyncMock(return_value=True)
        msg = make_message("bajojajo")
        await bajojajo_cmd.execute(msg)
        response = bajojajo_cmd.send_response.call_args[0][1]
        assert response.startswith("Ja ci dam bajo jajo, ")

    @pytest.mark.asyncio
    async def test_response_contains_an_address_from_minimal_fallback(self, bajojajo_cmd):
        bajojajo_cmd.send_response = AsyncMock(return_value=True)
        msg = make_message("bajojajo")
        await bajojajo_cmd.execute(msg)
        response = bajojajo_cmd.send_response.call_args[0][1]
        assert any(addr in response for addr in BajojajoCommand._MINIMAL_ADDRESS_FALLBACK)

    @pytest.mark.asyncio
    async def test_response_reuses_hello_commands_human_descriptors(self, bajojajo_cmd_with_hello):
        bajojajo_cmd_with_hello.send_response = AsyncMock(return_value=True)
        msg = make_message("bajojajo")
        hello_cmd = bajojajo_cmd_with_hello.bot.command_manager.commands['hello']
        descriptor_pool = hello_cmd.get_human_descriptors()

        await bajojajo_cmd_with_hello.execute(msg)
        response = bajojajo_cmd_with_hello.send_response.call_args[0][1]
        assert any(addr in response for addr in descriptor_pool)

    @pytest.mark.asyncio
    async def test_addresses_are_randomized_across_many_calls(self, bajojajo_cmd):
        bajojajo_cmd.send_response = AsyncMock(return_value=True)
        seen = set()
        for _ in range(40):
            msg = make_message("bajojajo")
            await bajojajo_cmd.execute(msg)
            seen.add(bajojajo_cmd.send_response.call_args[0][1])
        assert len(seen) > 1

    @pytest.mark.asyncio
    async def test_meme_suffix_never_appears_before_base_phrase(self, bajojajo_cmd):
        bajojajo_cmd.include_sender = False  # isolate from the sender-tag feature
        bajojajo_cmd.send_response = AsyncMock(return_value=True)
        for _ in range(40):
            msg = make_message("bajojajo")
            await bajojajo_cmd.execute(msg)
            response = bajojajo_cmd.send_response.call_args[0][1]
            assert response.startswith("Ja ci dam bajo jajo, ")
            if BajojajoCommand.MEME_SUFFIX_PL in response:
                assert response.endswith(BajojajoCommand.MEME_SUFFIX_PL)

    @pytest.mark.asyncio
    async def test_meme_suffix_never_repeated_within_one_reply(self, bajojajo_cmd):
        bajojajo_cmd.send_response = AsyncMock(return_value=True)
        for _ in range(40):
            msg = make_message("bajojajo")
            await bajojajo_cmd.execute(msg)
            response = bajojajo_cmd.send_response.call_args[0][1]
            assert response.count(BajojajoCommand.MEME_SUFFIX_PL) <= 1

    @pytest.mark.asyncio
    async def test_meme_suffix_is_optional_not_always_present(self, bajojajo_cmd):
        """Appears only occasionally, per spec -- across many calls we should
        see both with-suffix and without-suffix replies.
        """
        bajojajo_cmd.send_response = AsyncMock(return_value=True)
        with_suffix = without_suffix = 0
        for _ in range(60):
            msg = make_message("bajojajo")
            await bajojajo_cmd.execute(msg)
            response = bajojajo_cmd.send_response.call_args[0][1]
            if BajojajoCommand.MEME_SUFFIX_PL in response:
                with_suffix += 1
            else:
                without_suffix += 1
        assert with_suffix > 0
        assert without_suffix > 0

    @pytest.mark.asyncio
    async def test_meme_suffix_always_appended_when_forced(self, bajojajo_cmd, monkeypatch):
        monkeypatch.setattr(random, "random", lambda: 0.0)  # force suffix branch
        bajojajo_cmd.include_sender = False  # isolate from the sender-tag feature
        bajojajo_cmd.send_response = AsyncMock(return_value=True)
        msg = make_message("bajojajo")
        await bajojajo_cmd.execute(msg)
        response = bajojajo_cmd.send_response.call_args[0][1]
        assert response.startswith("Ja ci dam bajo jajo, ")
        assert response.endswith(f"! 💪 {BajojajoCommand.MEME_SUFFIX_PL}")

    def test_base_template_formats_without_error(self):
        formatted = BajojajoCommand.BASE_RESPONSE_PL.format(address="istoto testowa")
        assert "istoto testowa" in formatted
        assert formatted.startswith("Ja ci dam bajo jajo, ")


@pytest.mark.unit
class TestBajojajoDisabledConfiguration:
    """Covers spec item 13: feature disabled through configuration."""

    def test_can_execute_false_when_disabled(self, command_mock_bot):
        command_mock_bot.command_manager.commands = {}
        command_mock_bot.config.add_section('Bajojajo_Command')
        command_mock_bot.config.set('Bajojajo_Command', 'enabled', 'false')
        cmd = BajojajoCommand(command_mock_bot)
        msg = make_message("bajojajo")
        assert cmd.matches_custom_syntax(msg) is True  # still matches content...
        assert cmd.can_execute(msg) is False  # ...but is prevented from executing

    def test_can_execute_true_when_enabled(self, bajojajo_cmd):
        msg = make_message("bajojajo")
        assert bajojajo_cmd.can_execute(msg) is True


@pytest.mark.unit
class TestBajojajoConfigurableSettings:
    def test_custom_cooldown_from_config(self, command_mock_bot):
        command_mock_bot.command_manager.commands = {}
        command_mock_bot.config.add_section('Bajojajo_Command')
        command_mock_bot.config.set('Bajojajo_Command', 'cooldown_seconds', '5')
        cmd = BajojajoCommand(command_mock_bot)
        assert cmd.cooldown_seconds == 5


@pytest.mark.unit
class TestBajojajoExistingMessageHandlingUnaffected:
    """Covers spec item 14: unrelated commands/messages are unaffected."""

    def test_hello_keyword_message_does_not_trigger_bajojajo(self, bajojajo_cmd):
        assert bajojajo_cmd.matches_custom_syntax(make_message("hello there")) is False

    def test_bajojajo_command_has_no_leading_keywords(self, bajojajo_cmd):
        """Confirms it never competes with matches_keyword-based commands for
        a leading-token match; it only ever matches via matches_custom_syntax.
        """
        assert bajojajo_cmd.keywords == []
        assert bajojajo_cmd.matches_keyword(make_message("bajojajo")) is False
