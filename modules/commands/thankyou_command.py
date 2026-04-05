#!/usr/bin/env python3
"""
Thank You command for the MeshCore Bot
Responds to gratitude with Bender-themed sarcastic responses.
Only triggers in DMs or when the bot is @mentioned.
"""

import random
from typing import Any, List
from .base_command import BaseCommand
from ..models import MeshMessage


class ThankyouCommand(BaseCommand):
    """Handles thank-you messages with Bender-style responses.

    Hidden command — only responds in DMs or when bot is @mentioned,
    to avoid false triggers when users thank each other in channels.
    """

    # Plugin metadata
    name = "thankyou"
    keywords = [
        'thank you', 'thanks', 'thx', 'ty', 'thank',
        'thankyou', 'thnx', 'thnks',
    ]
    description = "Responds to gratitude with robot-themed responses"
    category = "hidden"
    cooldown_seconds = 3
    requires_dm = False
    requires_internet = False

    def __init__(self, bot: Any):
        super().__init__(bot)
        self.thankyou_enabled = self.get_config_value(
            'Thankyou_Command', 'enabled', fallback=True, value_type='bool'
        )

        # English fallback responses (Bender-style)
        self.responses_fallback = [
            "You're welcome. I know I'm amazing.",
            "No need to thank me. Actually, do. I deserve it.",
            "You're welcome, meatbag. Now where's my tip?",
            "I accept gratitude in the form of beer.",
            "Of course. I'm the greatest robot ever built.",
            "Don't mention it. Seriously, don't. It'll go to my head.",
            "You're welcome. Add it to the list of reasons I'm the best.",
            "Thanks are nice, but cash is nicer.",
            "I know, I know. I'm incredible. Tell me something new.",
            "Your gratitude has been logged and will be ignored.",
        ]

        # Shuffle bag for no-repeat rotation
        self._response_bag: List[str] = []

    def _get_responses(self) -> List[str]:
        """Get responses from translations or fallback."""
        responses = self.translate_get_value('commands.thankyou.responses')
        if responses and isinstance(responses, list) and len(responses) > 0:
            return responses
        return self.responses_fallback

    def _get_response(self) -> str:
        """Get a response using shuffle-bag (no repeats until all shown)."""
        if not self._response_bag:
            self._response_bag = list(self._get_responses())
            random.shuffle(self._response_bag)
        return self._response_bag.pop()

    def should_execute(self, message: MeshMessage) -> bool:
        """Only match in DMs or when bot is @mentioned."""
        if not super().should_execute(message):
            return False

        # DMs always allowed
        if message.is_dm:
            return True

        # In channels, only respond if bot is explicitly @mentioned
        return self._is_bot_mentioned(message.content)

    def can_execute(self, message: MeshMessage) -> bool:
        if not self.thankyou_enabled:
            return False
        return super().can_execute(message)

    def get_help_text(self) -> str:
        return ""

    async def execute(self, message: MeshMessage) -> bool:
        try:
            self.record_execution(message.sender_id)
            response = self._get_response()
            await self.send_response(message, response)
            return True
        except Exception as e:
            self.logger.error(f"Error in thankyou command: {e}")
            return True
