"""Shared interface + helpers for alert source modules.

An "alert source" is one push-worthy feed (IMGW, RSO, GIOS, PAA, ...) discovered and
driven by AlertsService (modules/service_plugins/alerts_service.py) -- see that file's
module docstring for the three push shapes (event/threshold/digest) and
AlertsService._discover_sources for how modules in this package get found and loaded.

To add a new source: drop a new module in this package with a class implementing
AlertSourceBase (usually via EventAlertSourceBase or ThresholdAlertSourceBase --
see those files), and it is picked up automatically. No changes to alerts_service.py,
no new [Alerts_Service] config keys beyond whatever the source itself defines.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, time
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from ..alerts_service import AlertsService

# Per-chunk budget (UTF-8 bytes) for splitting a long alert into several messages
# rather than truncating it -- same conservative ballpark CLAUDE.md documents for
# channel sends (~148 chars after the bot's username prefix). Byte-based (not a
# character count) since split_text_into_utf8_chunks is codepoint-safe and Polish
# diacritics/emoji are multi-byte in UTF-8.
MESSAGE_CHUNK_MAX_BYTES = 140

# IMGW/RSO timestamps carry seconds ("...:00"); PAA's don't ("HH:MM" only) -- tried
# in this order so the common (with-seconds) case is the fast path.
_DT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


class AlertSourceBase(ABC):
    """One alert source's config, scheduling, and push logic.

    Subclasses set `name` (also the config-key prefix, e.g. "imgw_meteo" ->
    imgw_meteo_enabled/imgw_meteo_template/...) and `schedule_kind` ("interval" or
    "daily"), and implement is_enabled()/check() (interval sources also implement
    poll_interval(); daily sources implement schedule_times() -- see
    EventAlertSourceBase / PaaSource for examples of each).
    """

    name: str
    schedule_kind: str = "interval"

    def __init__(self, service: AlertsService) -> None:
        self.service = service
        self.bot = service.bot
        self.logger = service.logger
        self._template_warning_logged: dict[str, bool] = {}

    @abstractmethod
    def is_enabled(self) -> bool:
        """Read f'{self.name}_enabled' (or equivalent) from [Alerts_Service]."""

    @abstractmethod
    async def check(self) -> None:
        """Fetch, process, and (maybe) send. Called once per poll/schedule tick."""

    def poll_interval(self) -> float:
        """Seconds between checks. Only called for schedule_kind == 'interval'."""
        raise NotImplementedError(f"{type(self).__name__} does not define poll_interval()")

    def schedule_times(self) -> list[time]:
        """Fixed daily times to check at. Only called for schedule_kind == 'daily'."""
        raise NotImplementedError(f"{type(self).__name__} does not define schedule_times()")

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def _send_simple_message(self, text: str) -> bool:
        """Send a single pre-built text, chunked (word/codepoint-safe split +
        TX-rate-limit-paced multi-send) if it doesn't fit in one message."""
        chunks = self.bot.command_manager.split_text_into_utf8_chunks(text, MESSAGE_CHUNK_MAX_BYTES)
        return await self.bot.command_manager.send_channel_messages_chunked(
            self.service.channel, chunks, scope=self.service.get_mesh_flood_scope()
        )

    # ------------------------------------------------------------------
    # Configurable message templates
    # ------------------------------------------------------------------

    def render_template(self, presets: dict[str, str], default_preset: str, **context: Any) -> str:
        """Resolve f'{self.name}_template' from config against `presets`.

        Resolution order: (1) a known preset name -> that preset's format string;
        (2) any other non-empty value -> used as a custom format string directly;
        (3) unset/empty -> `presets[default_preset]`. A bad template (references an
        unknown placeholder, or otherwise fails to .format()) is logged once and
        falls back to the default preset, so a typo in a custom template can't take
        the source down.

        Conditional/optional pieces of a message (e.g. "only show a date if one is
        present") are the caller's responsibility to pre-assemble into context
        values (e.g. a `date_part` key that's already " [10.08]" or "" -- see
        imgw_meteo.py/rso.py/gios.py) rather than expressed in the template string
        itself, so presets stay plain str.format() -- no template-language needed.
        """
        config_key = f"{self.name}_template"
        raw = (self.bot.config.get(self.service.config_section, config_key, fallback="") or "").strip()
        if raw in presets:
            template = presets[raw]
        elif raw:
            template = raw
        else:
            template = presets[default_preset]
        try:
            return template.format(**context)
        except (KeyError, ValueError, IndexError) as e:
            if not self._template_warning_logged.get(config_key):
                self.logger.warning(
                    "Invalid %s template %r: %s -- using the default preset instead",
                    config_key, raw, e,
                )
                self._template_warning_logged[config_key] = True
            return presets[default_preset].format(**context)

    # ------------------------------------------------------------------
    # Shared formatting/parsing helpers
    # ------------------------------------------------------------------

    def _translate(self, key: str, **kwargs: Any) -> str:
        translator = getattr(self.bot, "translator", None)
        if translator is None:
            return key
        return translator.translate(key, **kwargs)

    @staticmethod
    def _parse_dt(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        for fmt in _DT_FORMATS:
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
        return None

    @classmethod
    def _format_dt_short(cls, value: Optional[str]) -> Optional[str]:
        """'2026-08-10 19:00:00' -> '19:00 10.08'; returns None/raw on parse failure."""
        if not value:
            return None
        dt = cls._parse_dt(value)
        return dt.strftime("%H:%M %d.%m") if dt else value

    @classmethod
    def _format_date_short(cls, value: Optional[str]) -> Optional[str]:
        """'2026-08-10 19:00:00' -> '10.08'; returns None on missing/unparseable input."""
        dt = cls._parse_dt(value)
        return dt.strftime("%d.%m") if dt else None
