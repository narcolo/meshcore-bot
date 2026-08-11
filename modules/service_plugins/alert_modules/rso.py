"""RSO (Regionalny System Ostrzegania) alerts for a configured wojewodztwo
(default: podlaskie -- see rso_wojewodztwo) -- also carries republished Alert
RCB messages (there is no standalone live RCB feed, so this is the practical
integration point for those too). A discrete *event* source (see
event_source.py): "is this new?" dedup, polled on a fixed interval.
"""

from __future__ import annotations

import asyncio
from typing import Any

import requests

from ...clients import alert_sources
from .event_source import EventAlertSourceBase

# compact = current (pre-template-system) behavior, byte-for-byte: header + the
# full description merged into one text (unlike IMGW, RSO's description is
# normally short enough to matter for the default message). date_part/
# description_part are pre-built by _format_message (empty string when absent).
_TEMPLATES = {
    "compact": "📢 {prefix}{date_part}: {title}{description_part}",
    "minimal": "📢 {title}",
    "title_only": "📢 {prefix}: {title}",
}


class RsoSource(EventAlertSourceBase):
    name = "rso"

    def is_enabled(self) -> bool:
        return self.bot.config.getboolean(self.service.config_section, "rso_enabled", fallback=True)

    def poll_interval(self) -> float:
        return self.bot.config.getint(self.service.config_section, "rso_poll_interval", fallback=300)

    async def check(self) -> None:
        wojewodztwo = self.bot.config.get(self.service.config_section, "rso_wojewodztwo", fallback="podlaskie")

        loop = asyncio.get_event_loop()
        try:
            records = await loop.run_in_executor(None, alert_sources.fetch_rso_alerts, wojewodztwo)
        except requests.exceptions.RequestException as e:
            self.logger.warning("RSO request failed: %s", e)
            return
        except (ValueError, KeyError) as e:
            self.logger.warning("RSO response parse error: %s", e)
            return

        section = self.service.config_section
        send_details = self.bot.config.getboolean(section, "rso_send_details", fallback=False)
        max_alerts_per_hour = self.bot.config.getint(section, "rso_max_alerts_per_hour", fallback=12)
        max_age_hours = self.bot.config.getint(section, "rso_max_age_hours", fallback=24)

        await self._process_new_alerts(
            records, self._format_message, send_details, max_alerts_per_hour, max_age_hours
        )

    def _format_message(self, record: dict[str, Any]) -> str:
        prefix = self._translate("services.alerts.rso.prefix")
        title = record.get("title") or "?"
        description = record.get("description") or ""
        date = self._format_date_short(record.get("published_at"))
        date_part = f" [{date}]" if date else ""
        description_part = f" — {description}" if description and description != title else ""
        return self.render_template(
            _TEMPLATES, "compact",
            prefix=prefix, title=title, date=date or "", description=description,
            date_part=date_part, description_part=description_part,
        )
