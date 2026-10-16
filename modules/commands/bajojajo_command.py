#!/usr/bin/env python3
"""
Bajojajo command for the MeshCore Bot
Automatic Bender-style reaction to the Polish CB-radio "bajo jajo" meme.

Not a slash command: it has no leading-token keywords of its own (matched
via matches_custom_syntax instead) and reacts to the trigger phrase
appearing anywhere in ordinary channel/DM chatter -- the same pattern
ThankyouCommand uses for "thanks".
"""

import random
import re
from typing import Any, List, Optional

from .base_command import BaseCommand
from .hello_command import HelloCommand
from ..models import MeshMessage
from ..security_utils import sanitize_name


class BajojajoCommand(BaseCommand):
    """Reacts to 'bajojajo' / 'bajo jajo' anywhere in a message with a Bender quip.

    Hidden command -- fires automatically on any monitored channel/DM message
    containing the trigger phrase, regardless of how many times it appears
    (should_execute() is boolean, so exactly one reply is ever sent per message).
    """

    name = "bajojajo"
    keywords: List[str] = []  # matched via matches_custom_syntax (substring match), not matches_keyword
    description = "Automatic Bender-style reply to the 'bajo jajo' CB-radio meme"
    category = "hidden"
    cooldown_seconds = 20  # per-sender default; overridden from config in __init__
    requires_dm = False
    requires_internet = False

    DEFAULT_TRIGGERS = ["bajojajo", "bajo jajo"]

    # Last-resort address pool -- only used if the hello command's own
    # human_descriptors (translated or fallback) can't be reached at all.
    # See _get_address_pool(): the real vocabulary always comes from there.
    _MINIMAL_ADDRESS_FALLBACK = ["istoto", "człowieku", "istoto krzemowa"]

    RESPONSES_FALLBACK_PL = [
        "Ja ci dam bajo jajo, {address}! 💪",
        "Ja ci zaraz dam bajo jajo, {address}! 💪",
        "Na Inowrocławskiej jesteś?! Na Inowrocławskiej jesteś?",
        "Bajo jajo?! Ja ci zaraz dam, {address}! 🤖",
        "Spokojnie, {address}. Bajo jajo to nie dla takich jak ty.",
        "Ho, ho, {address}! Bajo jajo już w drodze!",
        "{address}, bajo jajo się nie przeterminowało. Ja ci dam!",
    ]

    # Sender tag prepended to the reply (channel messages only -- a DM's only
    # other party is already the sender, so no tag is needed there). Same
    # "@[name] " mention syntax as HelloCommand.SENDER_MENTION_FORMAT, kept as
    # its own prefix (not substituted into the {address} slot) since that slot
    # is the randomized Bender insult, not an identification mechanism.
    SENDER_MENTION_FORMAT = "@[{sender}] "

    settings_schema = [
        {"key": "triggers", "label": "Trigger phrases", "type": "list",
         "default": "bajojajo,bajo jajo",
         "help": "Comma-separated phrases to react to, matched anywhere in the "
                 "message body (case-insensitive, punctuation-agnostic)."},
        {"key": "cooldown_seconds", "label": "Per-sender cooldown (seconds)", "type": "int",
         "default": 20, "min": 0,
         "help": "Minimum seconds between replies to the same sender, so repeated "
                 "triggering doesn't spam the channel."},
        {"key": "include_sender", "label": "Tag the sender", "type": "bool",
         "default": True,
         "help": "Prefix the reply with @[sender] (channel messages only), so a "
                 "busy channel can tell whose message was answered."},
    ]

    short_description = "Automatic Bender reply to the 'bajo jajo' meme"
    usage = ""
    examples = ["bajojajo", "bajo jajo"]

    def __init__(self, bot: Any):
        super().__init__(bot)
        self.enabled = self.get_config_value(
            'Bajojajo_Command', 'enabled', fallback=True, value_type='bool'
        )
        self.cooldown_seconds = self.get_config_value(
            'Bajojajo_Command', 'cooldown_seconds', fallback=20, value_type='int'
        )
        self.include_sender = self.get_config_value(
            'Bajojajo_Command', 'include_sender', fallback=True, value_type='bool'
        )
        triggers = self.get_config_value(
            'Bajojajo_Command', 'triggers', fallback=None, value_type='list'
        )
        self._trigger_pattern = self._compile_trigger_pattern(triggers or self.DEFAULT_TRIGGERS)

    def get_help_text(self) -> str:
        return ""  # hidden automatic reaction, not a user-facing command

    @staticmethod
    def _compile_trigger_pattern(triggers: List[str]) -> Optional["re.Pattern[str]"]:
        """Build a single case-insensitive regex matching any configured trigger
        phrase anywhere in the text, with word boundaries so it doesn't fire
        inside an unrelated longer word, and flexible internal whitespace so a
        multi-word phrase matches with or without the space.
        """
        alternatives = []
        for trigger in triggers:
            trigger = (trigger or "").strip()
            if not trigger:
                continue
            tokens = trigger.split()
            if not tokens:
                continue
            alternatives.append(r"\s*".join(re.escape(t) for t in tokens))
        if not alternatives:
            return None
        pattern = r"(?<!\w)(?:" + "|".join(alternatives) + r")(?!\w)"
        return re.compile(pattern, re.IGNORECASE | re.UNICODE)

    def matches_custom_syntax(self, message: MeshMessage) -> bool:
        """Match the trigger phrase anywhere in the message body.

        Only ``message.content`` (the actual message text) is inspected --
        never the sender's nickname/display name, which message_handler
        already keeps separate from content for both channel ("Name: body")
        and DM messages. A single match is enough; occurrence count doesn't
        matter since should_execute() only ever sees True/False.

        ``@[...]`` mentions are stripped first (reusing BaseCommand's own
        mention handling), so addressing a user whose *name* happens to be
        "Bajojajo" (e.g. ``@[Bajojajo] hej``) doesn't itself count as a
        trigger -- only the keyword appearing in the actual written text does.
        """
        if not self._trigger_pattern:
            return False
        content = self._strip_mentions(message.content or "")
        return bool(self._trigger_pattern.search(content))

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        if not self.enabled:
            return False
        return super().can_execute(message, skip_channel_check=skip_channel_check)

    def _get_address_pool(self) -> List[str]:
        """Reuse the hello command's randomized vocative address generator.

        Prefers the live HelloCommand instance (so translations and any
        future tweaks to its vocabulary are picked up automatically), then
        falls back to the same translation key directly, then to a minimal
        built-in pool only if hello is completely unavailable.
        """
        commands = getattr(getattr(self.bot, 'command_manager', None), 'commands', None)
        hello_cmd = commands.get('hello') if isinstance(commands, dict) else None
        if hello_cmd is not None and hasattr(hello_cmd, 'get_human_descriptors'):
            try:
                descriptors = hello_cmd.get_human_descriptors()
                if descriptors and isinstance(descriptors, list):
                    return descriptors
            except Exception:
                pass

        descriptors = self.translate_get_value('commands.hello.human_descriptors')
        if descriptors and isinstance(descriptors, list) and len(descriptors) > 0:
            return descriptors

        return self._MINIMAL_ADDRESS_FALLBACK

    def _get_responses(self) -> List[str]:
        responses = self.translate_get_value('commands.bajojajo.responses')
        if responses and isinstance(responses, list) and len(responses) > 0:
            return responses
        return self.RESPONSES_FALLBACK_PL

    def _build_response(self) -> str:
        template = random.choice(self._get_responses())
        address = random.choice(self._get_address_pool())
        try:
            return template.format(address=address)
        except (KeyError, IndexError):
            return template

    def _get_sender_mention(self, message: MeshMessage) -> str:
        """Build the "@[name] " tag for the sender of *message*, or "" when the
        sender must not be named -- the setting is off, the reply is a DM (the
        recipient is the only other party), or the name is missing/unresolvable.

        Mirrors HelloCommand.get_sender_mention()'s sanitization exactly:
        channel sender names come straight off the wire unsanitized, so control
        characters, brackets (would let a crafted name forge a second mention)
        and braces (would break the str.format() the assembled reply still goes
        through) are stripped.
        """
        if not self.include_sender or message.is_dm:
            return ""

        sender = sanitize_name(message.sender_id or "")
        sender = sender.translate(str.maketrans("", "", "[]{}")).strip()
        if not sender or sender == HelloCommand.UNKNOWN_CHANNEL_SENDER:
            return ""

        return self.SENDER_MENTION_FORMAT.format(sender=sender)

    async def execute(self, message: MeshMessage) -> bool:
        response = self._build_response()
        mention = self._get_sender_mention(message)
        full_response = mention + response if mention else response

        # Nothing downstream truncates, so an over-budget reply is cut by the
        # firmware mid-word. A long node name can push the reply past the
        # channel body budget; drop the mention rather than lose the tail.
        if mention and len(full_response.encode('utf-8')) > self.get_max_message_length(message):
            full_response = response

        return await self.send_response(message, full_response)
