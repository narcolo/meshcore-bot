#!/usr/bin/env python3
"""
Tongue command for the MeshCore Bot
Responds to :P with :b and :b with :P
"""

from .base_command import BaseCommand
from ..models import MeshMessage


class TongueCommand(BaseCommand):
    """Responds to tongue-out emoticons."""

    name = "tongue"
    keywords = [':p', ':b']
    description = "Responds to :P with :b and vice versa"
    category = "fun"

    async def execute(self, message: MeshMessage) -> bool:
        text = message.content.strip().lower()
        if text == ':p':
            return await self.send_response(message, ":b")
        elif text == ':b':
            return await self.send_response(message, ":P")
        return False
