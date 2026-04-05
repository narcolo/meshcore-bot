#!/usr/bin/env python3
"""
Password command for the MeshCore Bot
Returns web viewer credentials via mesh message
"""

from .base_command import BaseCommand
from ..models import MeshMessage


class PasswordCommand(BaseCommand):
    """Returns the web viewer login credentials."""

    name = "password"
    keywords = ['password', 'pwd', 'hasło']
    description = "Get web viewer login credentials"
    category = "utility"

    short_description = "Get web viewer login credentials"
    usage = "password"
    examples = ["password", "pwd"]

    def __init__(self, bot):
        super().__init__(bot)
        self.enabled = self.get_config_value('Password_Command', 'enabled', fallback=True, value_type='bool')

    def can_execute(self, message: MeshMessage) -> bool:
        if not self.enabled:
            return False
        return super().can_execute(message)

    def get_help_text(self) -> str:
        return "Get web viewer login credentials"

    async def execute(self, message: MeshMessage) -> bool:
        # Check config override first
        config_pw = ''
        if self.bot.config.has_section('Web_Viewer'):
            config_pw = self.bot.config.get('Web_Viewer', 'auth_password', fallback='').strip()

        if config_pw.lower() == 'none':
            return await self.send_response(message, "Web viewer auth is disabled")

        password = config_pw if config_pw else None

        if not password:
            # Read from database
            password = self.bot.db_manager.get_metadata('web_password')

        if not password:
            return await self.send_response(message, "Web viewer password not yet generated")

        # Check for tunnel URL
        tunnel_url = self.bot.db_manager.get_metadata('web_tunnel_url')
        if tunnel_url:
            return await self.send_response(message, f"{tunnel_url} user: mesh pass: {password}")

        return await self.send_response(message, f"Web: user: mesh pass: {password}")
