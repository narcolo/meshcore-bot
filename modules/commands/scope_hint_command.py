#!/usr/bin/env python3
"""
Scope Hint command for the MeshCore Bot

Watches the configured channel (default Public) and warns senders whose
messages arrived as plain unscoped FLOOD to switch to scoped (TC_FLOOD)
routing. Transition-period nudge: the mesh is moving toward requiring
regional scopes, and unscoped messages will eventually be refused.

Invoked exclusively from MessageHandler._maybe_scope_hint() — never by the
generic command pipeline (automatic_hook_only = True). Fires only when
packet-level RF correlation confidently shows an unscoped FLOOD
(message.is_scoped_flood is False); unknown route types never warn.

Cooldown contract (user-approved 2026-07-18, best-effort): one warning per
cooldown_hours per companion, recorded only after a successful send and
persisted in bot_metadata so it survives restarts. Duplicates remain
possible after a crash between send and persist or a DB failure combined
with a restart; within one process the in-memory record always enforces
the window.
"""

import dataclasses
import time
from typing import Any, Optional

from .base_command import BaseCommand
from ..config_validation import PUBLIC_CHANNEL_OVERRIDE_KEY, _channel_name_is_public
from ..models import MeshMessage
from ..security_utils import validate_pubkey_format


class ScopeHintCommand(BaseCommand):
    """Warns senders on the configured channel who are using unscoped FLOOD."""

    # No keywords — triggered from MessageHandler._maybe_scope_hint() only
    name = "scope_hint"
    keywords = []
    description = "Warns senders on the configured channel using unscoped FLOOD to switch to scoped (TC_FLOOD) routing"
    category = "system"
    automatic_hook_only = True

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        # In-memory cooldown record: {identity: last_successful_send_epoch}.
        # Backed by bot_metadata for restart persistence (best-effort).
        self._notified: dict[str, float] = {}
        # Identities with a send currently in flight (closes the race where two
        # near-simultaneous messages both pass should_execute before either
        # send completes).
        self._in_flight: set[str] = set()
        self._load_config()

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _load_config(self) -> None:
        self.enabled = self.get_config_value(
            "Scope_Hint_Command", "enabled", fallback=False, value_type="bool"
        )
        # Channel to monitor — matched case-insensitively against message.channel.
        # Singular on purpose: a plural 'channels' key would populate
        # allowed_channels and open the channel to general command processing.
        self.public_channel = self.get_config_value(
            "Scope_Hint_Command", "channel", fallback="Public"
        ).strip()
        self.cooldown_hours = self.get_config_value(
            "Scope_Hint_Command", "cooldown_hours", fallback=24, value_type="int"
        )
        if self.cooldown_hours < 1:
            self.logger.warning(
                f"scope_hint: invalid cooldown_hours={self.cooldown_hours} (must be >= 1); using 24"
            )
            self.cooldown_hours = 24

        self.allow_unscoped_response = self.get_config_value(
            "Scope_Hint_Command", "allow_unscoped_response", fallback=False, value_type="bool"
        )
        self.effective_response_scope = self._resolve_effective_response_scope()

        # A warning about unscoped traffic must not itself go out as unscoped
        # global FLOOD unless the operator explicitly accepts that tradeoff.
        if self.enabled and not self.effective_response_scope and not self.allow_unscoped_response:
            self.logger.error(
                "scope_hint: no named response scope is in effect (set "
                "[Scope_Hint_Command] response_scope or [Channels] "
                "outgoing_flood_scope_override, or set allow_unscoped_response "
                "= true to accept sending the warning as global FLOOD). "
                "Disabling scope_hint."
            )
            self.enabled = False

        # Running on the shared Public channel requires the same explicit
        # override as monitor_channels does. Name-based check (config time);
        # scope_hint deliberately works outside monitor_channels, so it must
        # enforce this itself.
        if self.enabled and _channel_name_is_public(self.public_channel):
            override = self.bot.config.get(
                "Bot", PUBLIC_CHANNEL_OVERRIDE_KEY, fallback=""
            ).strip().lower()
            if override != "true":
                self.logger.error(
                    "scope_hint: channel is Public but the required override is "
                    "not set. Running a bot on Public is disruptive to other "
                    "mesh users. To override, add to [Bot]:\n"
                    f"  {PUBLIC_CHANNEL_OVERRIDE_KEY} = true\n"
                    "Disabling scope_hint."
                )
                self.enabled = False

    def _resolve_effective_response_scope(self) -> str:
        """Resolve the named scope for the warning send, or "" for global.

        Precedence (explicit beats fallback):
        - response_scope named           -> use it, normalized to the hash-less
          display form ("pl-podlasie"; a configured leading '#' is stripped —
          "name" and "#name" are the same region, and the '#' is applied only
          at key derivation, by meshcore-py / the firmware)
        - response_scope explicit global -> global ("" returned), even when the
          outgoing override names a scope
        - response_scope empty/absent    -> outgoing_flood_scope_override if it
          names a scope, else global
        """
        # Deferred import: commands are constructed inside CommandManager.__init__,
        # before bot.command_manager is assigned, so the staticmethod is used
        # directly (same normalization as the send path).
        from ..command_manager import CommandManager

        raw = (self.get_config_value("Scope_Hint_Command", "response_scope", fallback="") or "").strip()
        if raw:
            if MeshMessage.is_global_flood_scope(raw):
                return ""  # explicit global request
            return CommandManager._normalize_scope_name(raw)
        override = ""
        if self.bot.config.has_section("Channels") and self.bot.config.has_option(
            "Channels", "outgoing_flood_scope_override"
        ):
            override = (self.bot.config.get("Channels", "outgoing_flood_scope_override") or "").strip()
        if override and not MeshMessage.is_global_flood_scope(override):
            return CommandManager._normalize_scope_name(override)
        return ""

    # ------------------------------------------------------------------
    # Identity & cooldown
    # ------------------------------------------------------------------

    def _cooldown_identity(self, message: MeshMessage) -> Optional[str]:
        """Stable per-companion identity: full validated pubkey, else name.

        Only a full valid 64-hex-char public key earns the strong 'pk:'
        namespace — short prefixes and malformed strings are not
        collision-free companion identities. The 'name:' fallback is
        spoofable (group text is unverified); 'Channel User' is the
        handler's unparsed-sender fallback and never warrants a warning
        (not actionable, and all unidentified senders would share it).
        """
        pk = (message.sender_pubkey or "").strip().lower()
        if validate_pubkey_format(pk, expected_length=64):
            return f"pk:{pk}"
        name = (message.sender_id or "").strip()
        if not name or name == "Channel User":
            return None
        return f"name:{name.lower()}"

    def _cooldown_key(self, identity: str) -> str:
        return f"scope_hint_notified:{identity}"

    def _prune_expired_notified(self, now: float) -> None:
        window = self.cooldown_hours * 3600
        expired = [ident for ident, ts in self._notified.items() if (now - ts) >= window]
        for ident in expired:
            del self._notified[ident]

    def _is_on_cooldown(self, identity: str) -> bool:
        now = time.time()
        window = self.cooldown_hours * 3600
        last_mem = self._notified.get(identity)
        if last_mem is not None and (now - last_mem) < window:
            return True
        raw = self.bot.db_manager.get_metadata(self._cooldown_key(identity))
        if raw:
            try:
                last_db = float(raw)
            except (TypeError, ValueError):
                return False  # malformed row: treat as no cooldown
            if (now - last_db) < window:
                return True
        return False

    # ------------------------------------------------------------------
    # Guard logic
    # ------------------------------------------------------------------

    def should_execute(self, message: MeshMessage) -> bool:
        """Return True only when all conditions for a scope hint are met."""
        if not self.enabled:
            return False

        # Channel messages only — never reply in DMs
        if message.is_dm:
            return False

        # Must be the configured channel (case-insensitive)
        if not message.channel:
            return False
        if message.channel.lower() != self.public_channel.lower():
            return False

        # Must confidently know this was unscoped FLOOD. `is False` is
        # load-bearing: None (unknown / untrusted correlation) and True
        # (scoped TC_FLOOD) must NOT fire — `not x` would wrongly fire on None.
        if message.is_scoped_flood is not False:
            return False

        # Never reply to the bot's own messages
        bot_name = self.bot.config.get("Bot", "bot_name", fallback="Bot")
        if message.sender_id and message.sender_id.lower() == bot_name.lower():
            return False

        identity = self._cooldown_identity(message)
        if identity is None:
            return False

        if identity in self._in_flight:
            return False

        if self._is_on_cooldown(identity):
            self.logger.debug(f"scope_hint: {identity} is on cooldown, skipping")
            return False

        return True

    # ------------------------------------------------------------------
    # Payload building
    # ------------------------------------------------------------------

    def _payload_budget(self, hint_message: MeshMessage) -> int:
        """Max UTF-8 bytes for the hint body (strict physical limit).

        get_max_message_length() has a max(130, ...) floor that overestimates
        for bot usernames longer than 28 bytes (pre-existing concern, not
        changed here); the min() below keeps the physical packet arithmetic
        authoritative.
        """
        helper_budget = self.get_max_message_length(hint_message)
        username = self.bot.config.get("Bot", "bot_name", fallback="Bot")
        if hasattr(self.bot, "meshcore") and self.bot.meshcore:
            self_info = getattr(self.bot.meshcore, "self_info", None)
            if isinstance(self_info, dict):
                username = self_info.get("name") or self_info.get("user_name") or username
            elif self_info is not None:
                username = getattr(self_info, "name", None) or getattr(self_info, "user_name", None) or username
        physical = 160 - len(str(username).encode("utf-8")) - 2
        if not MeshMessage.is_global_flood_scope(hint_message.effective_outgoing_flood_scope(self.bot)):
            physical -= 10  # regional TC_FLOOD body overhead
        return min(helper_budget, physical)

    def _build_hint(self, message: MeshMessage, hint_message: MeshMessage) -> Optional[str]:
        """Build the warning text within the physical byte budget.

        Falls back from the named hint to the nameless hint_short (never
        truncates inside a @[name] mention); returns None when even the
        short form exceeds the budget (oversized configured scope or
        translation) — the caller must then skip the send entirely.
        """
        budget = self._payload_budget(hint_message)
        scope = self.effective_response_scope or ""
        name = (message.sender_id or "there").strip()

        full_hint = " ".join(
            self.translate("commands.scope_hint.hint", name=name, scope=scope).split()
        )
        if len(full_hint.encode("utf-8")) <= budget:
            return full_hint

        short_hint = " ".join(
            self.translate("commands.scope_hint.hint_short", scope=scope).split()
        )
        if len(short_hint.encode("utf-8")) <= budget:
            return short_hint

        self.logger.error(
            f"scope_hint: even hint_short exceeds the payload budget "
            f"({len(short_hint.encode('utf-8'))} > {budget} bytes); not sending. "
            "Check response_scope length and translation texts."
        )
        return None

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(self, message: MeshMessage) -> bool:
        """Send the scope hint on the channel (best-effort cooldown)."""
        # Re-check guard (race-condition safety)
        if not self.should_execute(message):
            return False

        identity = self._cooldown_identity(message)
        if identity is None or identity in self._in_flight:
            return False

        self._in_flight.add(identity)
        try:
            # Never mutate the incoming message: other command responses and
            # web-viewer capture must keep its true reply provenance.
            hint_message = message
            if self.effective_response_scope:
                hint_message = dataclasses.replace(message, reply_scope=self.effective_response_scope)

            hint = self._build_hint(message, hint_message)
            if hint is None:
                return False  # no send, no cooldown

            self.logger.info(
                f"scope_hint: notifying {identity} on {message.channel} (unscoped FLOOD detected)"
            )
            # Per-user admission bypassed (automated response) but the global
            # limiter still applies: the hint yields to real traffic. A send
            # rejected there is not recorded and retries on a later message.
            success = await self.send_response(
                hint_message, hint,
                skip_per_user_rate_limit=True,
                record_user_rate_limit=False,
            )
            if success:
                now = time.time()
                self._prune_expired_notified(now)
                self._notified[identity] = now
                self.bot.db_manager.set_metadata(self._cooldown_key(identity), str(now))
            return success
        finally:
            self._in_flight.discard(identity)
