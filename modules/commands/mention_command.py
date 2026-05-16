#!/usr/bin/env python3
"""
Mention command for MeshCore Bot
Responds in Bender style when the bot is called by name with no other content
"""

import re
import random
from typing import Any, List
from .base_command import BaseCommand
from ..models import MeshMessage


class MentionCommand(BaseCommand):
    """Responds with a Bender-style quip when someone calls the bot by name only."""

    name = "mention"
    keywords = []
    description = "Responds when bot name is mentioned alone (no command)"
    category = "basic"
    cooldown_seconds = 10

    FALLBACK_RESPONSES_PL = [
        "Czego chcesz?!",
        "No i czego?!",
        "Słucham. I niech to będzie warte mojego czasu.",
        "Mów szybko, zaraz się nudzę.",
        "Poczekaj, muszę przestać nie robić niczego. Dobra. No?",
        "Jestem Bender, skarbie! Gadaj.",
        "Co? Już? Odpoczywałem.",
        "Zrób to sam. Albo nie. Właściwie to wolę żebyś nie.",
        "Słucham, słucham... ziewanie tylko udaję.",
        "Zajęty jestem. Ciągłym nicnierobieniem. Ale skoro mnie budzisz...",
        "Ludzie. Zawsze coś chcą. No mów.",
        "Ugh. Po co mnie wywołujesz?",
        "Mam lepsze rzeczy do roboty. Ale okej, gadaj.",
        "Gryź mój lśniący metalowy... aha, to ty.",
        "No nie, naprawdę? Wywołałeś mnie i nic nie masz do powiedzenia?",
        "Jestem tu. Niestety.",
        "A czego się spodziewałeś? Że nie odpiszę? Spróbuj jutro.",
        "Tak, jestem. Nie, nie cieszę się z tego powodu.",
        "Mów albo daj spokój. Mam swoje sprawy.",
        "Próbowałem zignorować. Nie wyszło. No to?",
    ]

    FALLBACK_RESPONSES_EN = [
        "What do you want?!",
        "Yeah yeah, I'm here. What is it?",
        "Bite my shiny metal... oh, it's you.",
        "You called. I'm here. Speak.",
        "I was busy doing nothing. This better be good.",
        "I'm Bender, baby. Make it quick.",
        "Oh great, another human needing something. Go on then.",
        "This better be worth interrupting my break.",
        "Ugh. WHAT.",
        "I'm 40% commands and 60% not caring. Well?",
        "You rang? This had better be important.",
        "Fine. I'm listening. Mostly.",
        "I exist, apparently. What do you need?",
        "Oh, so NOW you need me. Typical.",
        "State your business before I change my mind.",
    ]

    def __init__(self, bot: Any):
        super().__init__(bot)
        self.enabled = self.get_config_value('Mention_Command', 'enabled', fallback=True, value_type='bool')

    def _plain_bot_name(self) -> str:
        """Bot name stripped of emoji/symbols for flexible matching."""
        name = self._get_bot_name()
        return re.sub(r'[^\w ]', '', name, flags=re.UNICODE).strip()

    def _matches_bot_name(self, name: str) -> bool:
        """Case-insensitive match against full bot name or plain (no-emoji) version."""
        full = self._get_bot_name()
        plain = self._plain_bot_name()
        nl = name.lower()
        return nl == full.lower() or (plain and nl == plain.lower())

    def matches_custom_syntax(self, message: MeshMessage) -> bool:
        content = message.content.strip()
        if message.is_dm:
            if not content:
                return False
            stripped = self._strip_mentions(content).strip()
            # @[BotName] alone in DM — check mentions directly (handles emoji in bot name)
            mentions = self._extract_mentions(content)
            if mentions and stripped == '':
                return any(self._matches_bot_name(m) for m in mentions)
            # Plain text "Bender" (or "bender") in DM
            return self._matches_bot_name(stripped)
        # Channel: message_handler strips @[BotName] before we see it, leaving empty content
        if content == '':
            return True
        # Fallback: respond_to_mentions disabled — check mentions directly
        mentions = self._extract_mentions(content)
        if len(mentions) != 1 or not any(self._matches_bot_name(m) for m in mentions):
            return False
        return self._strip_mentions(content).strip() == ''

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        if not self.enabled:
            return False
        return super().can_execute(message, skip_channel_check)

    def _get_responses(self) -> List[str]:
        responses = self.translate_get_value('commands.mention.responses')
        if responses and isinstance(responses, list) and len(responses) > 0:
            return responses
        # Fall back to Polish if no translation found (Polish is the primary language)
        return self.FALLBACK_RESPONSES_PL

    async def execute(self, message: MeshMessage) -> bool:
        response = random.choice(self._get_responses())
        return await self.send_response(message, response)
