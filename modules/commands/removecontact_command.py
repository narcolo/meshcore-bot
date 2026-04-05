#!/usr/bin/env python3
"""
Remove Contact Command
Allows admin users to delete contacts from the companion device via DM.
"""

from .base_command import BaseCommand
from ..models import MeshMessage


class RemoveContactCommand(BaseCommand):
    """Remove a contact from the companion device by name or pubkey prefix."""

    name = "removecontact"
    keywords = ["removecontact", "rmcontact", "delcontact"]
    description = "Remove a contact from the companion device"
    requires_dm = True
    cooldown_seconds = 2
    category = "hidden"

    def __init__(self, bot):
        super().__init__(bot)

    def can_execute(self, message: MeshMessage) -> bool:
        if not self.requires_admin_access():
            return False
        return super().can_execute(message)

    def requires_admin_access(self) -> bool:
        return True

    def get_help_text(self) -> str:
        return ""

    async def execute(self, message: MeshMessage) -> bool:
        parts = message.content.strip().split(None, 1)
        if len(parts) < 2:
            return await self.send_response(message, "Usage: removecontact <name or pubkey prefix>")

        query = parts[1].strip()

        mc = self.bot.meshcore
        if not mc or not mc.is_connected:
            return await self.send_response(message, "Not connected")

        # Refresh contacts from device
        await mc.commands.get_contacts()

        # Try by name first, then by pubkey prefix
        contact = mc.get_contact_by_name(query)
        if not contact:
            contact = mc.get_contact_by_key_prefix(query)

        if not contact:
            return await self.send_response(message, f"Contact not found: {query}")

        name = contact.get("adv_name", "<unnamed>")
        pubkey = contact.get("public_key", "")

        result = await mc.commands.remove_contact(pubkey)

        from meshcore.events import EventType
        if result.type == EventType.OK:
            return await self.send_response(message, f"Removed: {name} ({pubkey[:12]}...)")
        else:
            return await self.send_response(message, f"Failed to remove {name}: {result.type}")
