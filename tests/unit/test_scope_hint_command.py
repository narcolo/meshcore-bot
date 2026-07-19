#!/usr/bin/env python3
"""
Unit tests for ScopeHintCommand.

Covers:
  - should_execute() filtering (enabled, channel, is_scoped_flood trichotomy,
    bot self, identity resolution, cooldown, DM)
  - identity namespacing (pk: vs name:) and pubkey validation
  - effective response-scope precedence and force-disable rules
  - execute() cooldown semantics (successful-send only), _in_flight guard,
    no mutation of the incoming message
  - payload budget: EN/PL physical byte limits, hint_short fallback,
    terminal None on oversized scope/translation
  - restart persistence against a real temp-SQLite DBManager
"""

import configparser
import dataclasses
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from modules.commands.scope_hint_command import ScopeHintCommand
from modules.config_validation import PUBLIC_CHANNEL_OVERRIDE_KEY
from modules.db_manager import DBManager
from modules.models import MeshMessage

pytestmark = pytest.mark.unit

VALID_PUBKEY = "ab" * 32  # 64 hex chars
OTHER_PUBKEY = "cd" * 32

_TRANSLATIONS = {
    lang: json.loads(
        (Path(__file__).resolve().parents[2] / "translations" / f"{lang}.json").read_text(encoding="utf-8")
    )
    for lang in ("en", "pl")
}


def _real_hint(lang: str, key: str, **kwargs) -> str:
    """Format the actual shipped translation string (for payload-size tests)."""
    template = _TRANSLATIONS[lang]["commands"]["scope_hint"][key]
    return template.format(**kwargs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bot(
    enabled: bool = True,
    bot_name: str = "TestBot",
    channel: str = "general",
    cooldown_hours: str = "24",
    response_scope: str = "pl-podlasie",
    allow_unscoped_response: str = "false",
    outgoing_override: str = "",
    public_override: bool = False,
    lang: str = "en",
) -> MagicMock:
    config = configparser.ConfigParser()
    config.optionxform = str  # preserve case for the long override key
    config.add_section("Bot")
    config.set("Bot", "bot_name", bot_name)
    if public_override:
        config.set("Bot", PUBLIC_CHANNEL_OVERRIDE_KEY, "true")
    config.add_section("Channels")
    config.set("Channels", "monitor_channels", "general")
    if outgoing_override:
        config.set("Channels", "outgoing_flood_scope_override", outgoing_override)
    config.add_section("Scope_Hint_Command")
    config.set("Scope_Hint_Command", "enabled", str(enabled).lower())
    config.set("Scope_Hint_Command", "channel", channel)
    config.set("Scope_Hint_Command", "cooldown_hours", cooldown_hours)
    config.set("Scope_Hint_Command", "response_scope", response_scope)
    config.set("Scope_Hint_Command", "allow_unscoped_response", allow_unscoped_response)

    bot = MagicMock()
    bot.config = config
    bot.logger = MagicMock()

    def _translate(key, **kwargs):
        parts = key.split(".")
        node = _TRANSLATIONS[lang]
        for part in parts:
            node = node[part]
        return node.format(**kwargs)

    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=_translate)
    bot.translator.get_value = Mock(return_value=None)

    bot.db_manager = MagicMock()
    bot.db_manager.get_metadata = Mock(return_value=None)
    bot.db_manager.set_metadata = Mock()

    bot.command_manager = MagicMock()
    bot.command_manager.send_response = AsyncMock(return_value=True)

    # meshcore.self_info drives the payload budget's username lookup
    bot.meshcore = MagicMock()
    bot.meshcore.self_info = {"name": bot_name}
    return bot


def _make_command(bot=None, **bot_kwargs) -> ScopeHintCommand:
    if bot is None:
        bot = _make_bot(**bot_kwargs)
    return ScopeHintCommand(bot)


def _msg(
    sender_id: str = "Alice",
    channel: str = "general",
    is_dm: bool = False,
    is_scoped_flood=False,
    sender_pubkey: str = "",
) -> MeshMessage:
    return MeshMessage(
        content="hello mesh",
        sender_id=sender_id,
        sender_pubkey=sender_pubkey,
        channel=channel if not is_dm else None,
        is_dm=is_dm,
        is_scoped_flood=is_scoped_flood,
    )


# ---------------------------------------------------------------------------
# should_execute
# ---------------------------------------------------------------------------

class TestShouldExecute:
    def test_true_for_valid_unscoped_message(self):
        cmd = _make_command()
        assert cmd.should_execute(_msg(is_scoped_flood=False)) is True

    def test_false_when_disabled(self):
        cmd = _make_command(enabled=False)
        assert cmd.should_execute(_msg()) is False

    def test_false_for_dm(self):
        cmd = _make_command()
        assert cmd.should_execute(_msg(is_dm=True)) is False

    def test_false_for_wrong_channel(self):
        cmd = _make_command()
        assert cmd.should_execute(_msg(channel="other")) is False

    def test_channel_match_is_case_insensitive(self):
        cmd = _make_command(channel="General")
        assert cmd.should_execute(_msg(channel="gEnErAl")) is True

    def test_false_when_scoped_flood(self):
        cmd = _make_command()
        assert cmd.should_execute(_msg(is_scoped_flood=True)) is False

    def test_false_when_route_type_unknown(self):
        # None (failed/untrusted correlation) must never fire — `is False` guard
        cmd = _make_command()
        assert cmd.should_execute(_msg(is_scoped_flood=None)) is False

    def test_false_when_sender_is_bot(self):
        cmd = _make_command(bot_name="TestBot")
        assert cmd.should_execute(_msg(sender_id="TestBot")) is False

    def test_bot_name_comparison_case_insensitive(self):
        cmd = _make_command(bot_name="TestBot")
        assert cmd.should_execute(_msg(sender_id="tEsTbOt")) is False

    def test_false_when_sender_empty(self):
        cmd = _make_command()
        assert cmd.should_execute(_msg(sender_id="")) is False

    def test_false_when_sender_whitespace(self):
        cmd = _make_command()
        assert cmd.should_execute(_msg(sender_id="   ")) is False

    def test_false_for_channel_user_fallback(self):
        # Handler's unparsed-sender default: not actionable, shared by everyone
        cmd = _make_command()
        assert cmd.should_execute(_msg(sender_id="Channel User")) is False

    def test_channel_user_with_valid_pubkey_still_fires(self):
        cmd = _make_command()
        msg = _msg(sender_id="Channel User", sender_pubkey=VALID_PUBKEY)
        assert cmd.should_execute(msg) is True

    def test_false_when_on_cooldown(self):
        cmd = _make_command()
        cmd.bot.db_manager.get_metadata = Mock(return_value=str(time.time()))
        assert cmd.should_execute(_msg()) is False

    def test_true_after_cooldown_expired(self):
        cmd = _make_command()
        expired = time.time() - 25 * 3600
        cmd.bot.db_manager.get_metadata = Mock(return_value=str(expired))
        assert cmd.should_execute(_msg()) is True

    def test_malformed_stored_value_treated_as_no_cooldown(self):
        cmd = _make_command()
        cmd.bot.db_manager.get_metadata = Mock(return_value="not-a-number")
        assert cmd.should_execute(_msg()) is True

    def test_in_memory_cooldown_blocks_without_db(self):
        cmd = _make_command()
        cmd._notified["name:alice"] = time.time()
        assert cmd.should_execute(_msg(sender_id="Alice")) is False

    def test_in_flight_identity_blocks(self):
        cmd = _make_command()
        cmd._in_flight.add("name:alice")
        assert cmd.should_execute(_msg(sender_id="Alice")) is False

    def test_invalid_cooldown_hours_falls_back_to_24(self):
        cmd = _make_command(cooldown_hours="0")
        assert cmd.cooldown_hours == 24
        cmd = _make_command(cooldown_hours="-5")
        assert cmd.cooldown_hours == 24


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

class TestCooldownIdentity:
    def test_full_valid_pubkey_gets_pk_namespace(self):
        cmd = _make_command()
        assert cmd._cooldown_identity(_msg(sender_pubkey=VALID_PUBKEY)) == f"pk:{VALID_PUBKEY}"

    def test_pubkey_case_normalized(self):
        cmd = _make_command()
        upper = VALID_PUBKEY.upper()
        assert cmd._cooldown_identity(_msg(sender_pubkey=upper)) == f"pk:{VALID_PUBKEY}"

    def test_short_pubkey_prefix_falls_back_to_name(self):
        cmd = _make_command()
        assert cmd._cooldown_identity(_msg(sender_id="Alice", sender_pubkey="ab" * 6)) == "name:alice"

    def test_63_and_65_char_keys_rejected(self):
        cmd = _make_command()
        assert cmd._cooldown_identity(_msg(sender_pubkey="a" * 63)) == "name:alice"
        assert cmd._cooldown_identity(_msg(sender_pubkey="a" * 65)) == "name:alice"

    def test_64_char_non_hex_rejected(self):
        cmd = _make_command()
        assert cmd._cooldown_identity(_msg(sender_pubkey="zz" * 32)) == "name:alice"

    def test_name_lowercased(self):
        cmd = _make_command()
        assert cmd._cooldown_identity(_msg(sender_id="AlIcE")) == "name:alice"

    def test_channel_user_yields_none(self):
        cmd = _make_command()
        assert cmd._cooldown_identity(_msg(sender_id="Channel User")) is None

    def test_empty_sender_yields_none(self):
        cmd = _make_command()
        assert cmd._cooldown_identity(_msg(sender_id="")) is None

    def test_same_name_different_pubkeys_independent(self):
        cmd = _make_command()
        id1 = cmd._cooldown_identity(_msg(sender_id="Alice", sender_pubkey=VALID_PUBKEY))
        id2 = cmd._cooldown_identity(_msg(sender_id="Alice", sender_pubkey=OTHER_PUBKEY))
        assert id1 != id2


# ---------------------------------------------------------------------------
# Effective response scope
# ---------------------------------------------------------------------------

class TestEffectiveResponseScope:
    def test_named_scope_kept_hashless(self):
        # Hash-less display form is canonical throughout the bot
        cmd = _make_command(response_scope="pl-podlasie")
        assert cmd.effective_response_scope == "pl-podlasie"

    def test_hash_spelling_normalized_to_hashless(self):
        # "#name" and "name" are the same region (firmware/meshcore-py prepend
        # '#' only at key derivation); config accepts both, display drops '#'
        cmd = _make_command(response_scope="#pl-podlasie")
        assert cmd.effective_response_scope == "pl-podlasie"

    @pytest.mark.parametrize("marker", ["*", "0", "None"])
    def test_explicit_global_beats_named_override_and_disables(self, marker):
        cmd = _make_command(response_scope=marker, outgoing_override="#west")
        assert cmd.effective_response_scope == ""
        assert cmd.enabled is False  # allow_unscoped_response defaults to false

    def test_empty_scope_falls_back_to_outgoing_override(self):
        cmd = _make_command(response_scope="", outgoing_override="#west")
        assert cmd.effective_response_scope == "west"  # normalized hash-less
        assert cmd.enabled is True

    def test_empty_scope_and_global_override_disables(self):
        cmd = _make_command(response_scope="", outgoing_override="*")
        assert cmd.effective_response_scope == ""
        assert cmd.enabled is False

    def test_no_named_scope_with_allow_unscoped_stays_enabled(self):
        cmd = _make_command(response_scope="", allow_unscoped_response="true")
        assert cmd.effective_response_scope == ""
        assert cmd.enabled is True

    def test_whitespace_scope_treated_as_empty(self):
        cmd = _make_command(response_scope="   ", allow_unscoped_response="true")
        assert cmd.effective_response_scope == ""


# ---------------------------------------------------------------------------
# Public-channel authorization
# ---------------------------------------------------------------------------

class TestPublicChannelGuard:
    def test_public_without_override_force_disabled(self):
        cmd = _make_command(channel="Public", public_override=False)
        assert cmd.enabled is False

    def test_public_with_override_stays_enabled(self):
        cmd = _make_command(channel="Public", public_override=True)
        assert cmd.enabled is True

    def test_public_name_variants_detected(self):
        cmd = _make_command(channel="#public", public_override=False)
        assert cmd.enabled is False

    def test_non_public_channel_needs_no_override(self):
        cmd = _make_command(channel="general", public_override=False)
        assert cmd.enabled is True


# ---------------------------------------------------------------------------
# execute()
# ---------------------------------------------------------------------------

class TestExecute:
    async def test_sends_hint_and_records_cooldown(self):
        cmd = _make_command()
        msg = _msg(sender_id="Alice")
        result = await cmd.execute(msg)
        assert result is True
        send = cmd.bot.command_manager.send_response
        send.assert_awaited_once()
        sent_content = send.await_args.args[1]
        assert "Alice" in sent_content
        assert "pl-podlasie" in sent_content
        # rate-limit contract: per-user admission bypassed, per-user accounting off
        assert send.await_args.kwargs["skip_per_user_rate_limit"] is True
        assert send.await_args.kwargs["record_user_rate_limit"] is False
        # cooldown recorded in memory and DB
        assert "name:alice" in cmd._notified
        cmd.bot.db_manager.set_metadata.assert_called_once()
        key, value = cmd.bot.db_manager.set_metadata.call_args.args
        assert key == "scope_hint_notified:name:alice"
        assert float(value) == pytest.approx(time.time(), abs=5)

    async def test_failed_send_stores_no_cooldown(self):
        cmd = _make_command()
        cmd.bot.command_manager.send_response = AsyncMock(return_value=False)
        result = await cmd.execute(_msg(sender_id="Alice"))
        assert result is False
        assert "name:alice" not in cmd._notified
        cmd.bot.db_manager.set_metadata.assert_not_called()

    async def test_noop_when_guard_fails(self):
        cmd = _make_command()
        result = await cmd.execute(_msg(is_scoped_flood=True))
        assert result is False
        cmd.bot.command_manager.send_response.assert_not_awaited()
        cmd.bot.db_manager.set_metadata.assert_not_called()

    async def test_original_message_not_mutated(self):
        cmd = _make_command(response_scope="pl-podlasie")
        msg = _msg(sender_id="Alice")
        assert msg.reply_scope is None
        await cmd.execute(msg)
        assert msg.reply_scope is None  # copy carries the scope, not the original
        hint_message = cmd.bot.command_manager.send_response.await_args.args[0]
        assert hint_message.reply_scope == "pl-podlasie"
        assert hint_message is not msg

    async def test_unscoped_response_leaves_reply_scope(self):
        cmd = _make_command(response_scope="", allow_unscoped_response="true")
        msg = _msg(sender_id="Alice")
        await cmd.execute(msg)
        hint_message = cmd.bot.command_manager.send_response.await_args.args[0]
        assert hint_message.reply_scope is None

    async def test_in_flight_blocks_concurrent_double_fire(self):
        cmd = _make_command()
        release = []

        async def _slow_send(*args, **kwargs):
            while not release:
                import asyncio
                await asyncio.sleep(0.01)
            return True

        cmd.bot.command_manager.send_response = AsyncMock(side_effect=_slow_send)
        import asyncio
        task1 = asyncio.create_task(cmd.execute(_msg(sender_id="Alice")))
        await asyncio.sleep(0.02)  # task1 enters _in_flight
        task2 = asyncio.create_task(cmd.execute(_msg(sender_id="Alice")))
        await asyncio.sleep(0.02)
        release.append(True)
        r1, r2 = await asyncio.gather(task1, task2)
        assert sorted([r1, r2]) == [False, True]
        assert cmd.bot.command_manager.send_response.await_count == 1

    async def test_send_exception_swallowed_no_cooldown_in_flight_cleared(self):
        # BaseCommand.send_response catches transport exceptions and returns
        # False; execute() must then record nothing and release the identity.
        cmd = _make_command()
        cmd.bot.command_manager.send_response = AsyncMock(side_effect=RuntimeError("radio down"))
        result = await cmd.execute(_msg(sender_id="Alice"))
        assert result is False
        assert "name:alice" not in cmd._notified
        assert "name:alice" not in cmd._in_flight
        cmd.bot.db_manager.set_metadata.assert_not_called()

    async def test_set_metadata_failure_leaves_memory_record(self):
        cmd = _make_command()
        cmd.bot.db_manager.set_metadata = Mock(side_effect=Exception("disk full"))
        with pytest.raises(Exception):
            await cmd.execute(_msg(sender_id="Alice"))
        # memory record installed before the DB write attempt
        assert "name:alice" in cmd._notified

    async def test_prunes_expired_memory_entries(self):
        cmd = _make_command()
        cmd._notified["name:old"] = time.time() - 48 * 3600
        await cmd.execute(_msg(sender_id="Alice"))
        assert "name:old" not in cmd._notified
        assert "name:alice" in cmd._notified


# ---------------------------------------------------------------------------
# Payload budget
# ---------------------------------------------------------------------------

LONG_NAME_31 = "ąęółżźćń" + "x" * 15  # 8 two-byte chars = 16 bytes + 15 = 31 UTF-8 bytes
LONG_BOT_31 = "Ż" * 15 + "y"          # 15*2 + 1 = 31 UTF-8 bytes


class TestPayloadBudget:
    @pytest.mark.parametrize("lang", ["en", "pl"])
    def test_hint_fits_physical_budget_worst_case(self, lang):
        assert len(LONG_NAME_31.encode("utf-8")) == 31
        assert len(LONG_BOT_31.encode("utf-8")) == 31
        cmd = _make_command(bot_name=LONG_BOT_31, lang=lang)
        msg = _msg(sender_id=LONG_NAME_31)
        hint_message = dataclasses.replace(msg, reply_scope="pl-podlasie")
        hint = cmd._build_hint(msg, hint_message)
        assert hint is not None
        assert LONG_NAME_31 in hint  # full hint used, no fallback needed
        physical = 160 - 31 - 2 - 10
        assert len(hint.encode("utf-8")) <= physical

    @pytest.mark.parametrize("lang", ["en", "pl"])
    def test_oversize_name_falls_back_to_hint_short(self, lang):
        cmd = _make_command(bot_name=LONG_BOT_31, lang=lang)
        huge_name = "Ą" * 60  # 120 bytes: hint cannot fit
        msg = _msg(sender_id=huge_name)
        hint_message = dataclasses.replace(msg, reply_scope="pl-podlasie")
        hint = cmd._build_hint(msg, hint_message)
        assert hint is not None
        assert huge_name not in hint  # nameless short form, no broken mention
        physical = 160 - 31 - 2 - 10
        assert len(hint.encode("utf-8")) <= physical

    def test_oversized_scope_returns_none_and_no_send(self):
        bot = _make_bot(response_scope="#" + "x" * 200)
        cmd = ScopeHintCommand(bot)
        msg = _msg(sender_id="Alice")
        hint_message = dataclasses.replace(msg, reply_scope=cmd.effective_response_scope)
        assert cmd._build_hint(msg, hint_message) is None

    async def test_oversized_scope_records_no_cooldown(self):
        bot = _make_bot(response_scope="#" + "x" * 200)
        cmd = ScopeHintCommand(bot)
        result = await cmd.execute(_msg(sender_id="Alice"))
        assert result is False
        cmd.bot.command_manager.send_response.assert_not_awaited()
        cmd.bot.db_manager.set_metadata.assert_not_called()

    def test_budget_uses_min_of_helper_and_physical(self):
        # 31-byte bot name: helper floor gives 130-10=120, physics 117
        cmd = _make_command(bot_name=LONG_BOT_31)
        msg = _msg(sender_id="Alice")
        hint_message = dataclasses.replace(msg, reply_scope="pl-podlasie")
        assert cmd._payload_budget(hint_message) == 160 - 31 - 2 - 10

    def test_budget_without_scope_skips_regional_overhead(self):
        cmd = _make_command(bot_name="Bot", response_scope="", allow_unscoped_response="true")
        msg = _msg(sender_id="Alice")
        assert cmd._payload_budget(msg) == 160 - 3 - 2

    def test_empty_scope_collapses_whitespace(self):
        cmd = _make_command(response_scope="", allow_unscoped_response="true")
        msg = _msg(sender_id="Alice")
        hint = cmd._build_hint(msg, msg)
        assert hint is not None
        assert "  " not in hint


# ---------------------------------------------------------------------------
# Restart persistence (real SQLite via DBManager)
# ---------------------------------------------------------------------------

class TestRestartPersistence:
    def _bot_with_real_db(self, tmp_path, filename="scope_hint.db"):
        bot = _make_bot()
        db_bot = Mock()
        db_bot.logger = MagicMock()
        bot.db_manager = DBManager(db_bot, str(tmp_path / filename))
        return bot

    async def test_cooldown_survives_restart(self, tmp_path):
        bot_a = self._bot_with_real_db(tmp_path)
        cmd_a = ScopeHintCommand(bot_a)
        assert await cmd_a.execute(_msg(sender_id="Alice")) is True

        # "Restart": fresh command instance + fresh DBManager on the same file
        bot_b = self._bot_with_real_db(tmp_path)
        cmd_b = ScopeHintCommand(bot_b)
        assert cmd_b._notified == {}  # nothing in memory
        assert cmd_b.should_execute(_msg(sender_id="Alice")) is False  # DB blocks

        # A different companion is unaffected
        assert cmd_b.should_execute(_msg(sender_id="Bob")) is True

    async def test_expired_persisted_cooldown_allows_warning(self, tmp_path):
        bot_a = self._bot_with_real_db(tmp_path)
        cmd_a = ScopeHintCommand(bot_a)
        expired = time.time() - 25 * 3600
        bot_a.db_manager.set_metadata("scope_hint_notified:name:alice", str(expired))
        assert cmd_a.should_execute(_msg(sender_id="Alice")) is True
