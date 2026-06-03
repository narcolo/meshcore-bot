#!/usr/bin/env python3
"""
Pikachu command for the MeshCore Bot
Easter egg ASCII art response
"""

from .base_command import BaseCommand
from ..models import MeshMessage

PIKACHU = "  (|)෴(|)\n ϟ ◕ ˕ ◕ ϟ ฅ\n  Pika pika!"


class PikachuCommand(BaseCommand):
    """Responds to 'pika' or 'pikachu' with ASCII art."""

    name = "pikachu"
    keywords = ['pika', 'pikachu']
    description = "Pika pika!"
    category = "hidden"

    async def execute(self, message: MeshMessage) -> bool:
        return await self.send_response(message, PIKACHU)
