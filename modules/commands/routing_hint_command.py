#!/usr/bin/env python3
"""
Routing Hint command for the MeshCore Bot

Monitors the configured Public channel and notifies users who are transmitting
with 1-byte routing, asking them to switch to 2-byte routing and explaining how.
Triggered automatically from process_message() — no keyword required.
"""

import time
from typing import Any, Dict

from .base_command import BaseCommand
from ..models import MeshMessage


class RoutingHintCommand(BaseCommand):
    """Notifies Public-channel users who are using 1-byte routing."""

    # No keywords — triggered automatically from message_handler.process_message()
    name = "routing_hint"
    keywords = []
    description = "Notifies users on the Public channel who are using 1-byte routing to switch to 2-byte routing"
    category = "system"

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._load_config()
        # In-memory per-user cooldown: {sender_id: last_notified_epoch}
        self._notified: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _load_config(self) -> None:
        self.enabled = self.get_config_value(
            "Routing_Hint_Command", "enabled", fallback=False, value_type="bool"
        )
        # Channel to monitor — matched case-insensitively against message.channel
        self.public_channel = self.get_config_value(
            "Routing_Hint_Command", "channel", fallback="Public"
        ).strip()
        # Hours between repeat notifications to the same user (avoid spam)
        self.cooldown_hours = self.get_config_value(
            "Routing_Hint_Command", "cooldown_hours", fallback=24, value_type="int"
        )

    # ------------------------------------------------------------------
    # Guard logic
    # ------------------------------------------------------------------

    def should_execute(self, message: MeshMessage) -> bool:
        """Return True only when all conditions for a routing hint are met."""
        if not self.enabled:
            return False

        # Channel messages only — never reply in DMs
        if message.is_dm:
            return False

        # Must be the configured public channel (case-insensitive)
        if not message.channel:
            return False
        if message.channel.lower() != self.public_channel.lower():
            return False

        # Routing info must be present and show 1 byte per hop
        if not message.routing_info:
            return False
        if message.routing_info.get("bytes_per_hop", 2) != 1:
            return False

        # Never reply to the bot's own messages
        bot_name = self.bot.config.get("Bot", "bot_name", fallback="Bot")
        if message.sender_id and message.sender_id.lower() == bot_name.lower():
            return False

        # Skip messages with no usable sender name
        if not message.sender_id or not message.sender_id.strip():
            return False

        # Per-user cooldown — avoids spamming the same user
        if self._is_on_cooldown(message.sender_id):
            self.logger.debug(
                f"routing_hint: {message.sender_id} is on cooldown, skipping"
            )
            return False

        return True

    def _is_on_cooldown(self, sender_id: str) -> bool:
        last = self._notified.get(sender_id)
        if last is None:
            return False
        return (time.time() - last) < self.cooldown_hours * 3600

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(self, message: MeshMessage) -> bool:
        """Send a two-part routing hint on the public channel."""
        # Re-check guard (race-condition safety — mirrors greeter pattern)
        if not self.should_execute(message):
            return False

        # Record immediately to prevent duplicate sends
        self._notified[message.sender_id] = time.time()

        name = (message.sender_id or "there").strip()

        part1 = self.translate("commands.routing_hint.hint_part1", name=name)
        part2 = self.translate("commands.routing_hint.hint_part2")

        self.logger.info(
            f"routing_hint: notifying {name} on {message.channel} "
            f"(1-byte routing detected)"
        )
        return await self.send_response_chunked(message, [part1, part2])
