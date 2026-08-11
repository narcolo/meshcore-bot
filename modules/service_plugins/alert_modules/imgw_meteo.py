"""IMGW meteorological warnings, filtered to warnings touching Bialystok/powiat
bialostocki -- a discrete *event* source (see event_source.py): "is this new?"
dedup, polled on a fixed interval.
"""

from __future__ import annotations

import asyncio
from typing import Any

import requests

from ...clients import alert_sources
from .event_source import EventAlertSourceBase

# compact = current (pre-template-system) behavior, byte-for-byte: header only,
# full warning text (tresc) sent separately via send_details since it's usually
# much longer. date_part/until_part are pre-built by _format_message (empty
# string when absent) so this stays a plain str.format() template -- no
# conditional syntax needed, and no dangling "[]"/"—" when a warning lacks a
# date or expiry.
_TEMPLATES = {
    "compact": "⚠️ {prefix}: {title} (st.{severity}){date_part}{until_part}",
    "minimal": "⚠️ {title} st.{severity}",
    "detailed": "⚠️ {prefix}: {title} (st.{severity}){date_part}{until_part}\n{description}",
}


class ImgwMeteoSource(EventAlertSourceBase):
    name = "imgw_meteo"

    def is_enabled(self) -> bool:
        return self.bot.config.getboolean(self.service.config_section, "imgw_meteo_enabled", fallback=True)

    def poll_interval(self) -> float:
        return self.bot.config.getint(self.service.config_section, "imgw_meteo_poll_interval", fallback=300)

    def _teryt_codes(self) -> set[str]:
        raw = self.bot.config.get(self.service.config_section, "imgw_meteo_teryt_codes", fallback="2061,2002")
        codes = {c.strip() for c in raw.split(",") if c.strip()}
        return codes or alert_sources.BIALYSTOK_TERYT

    async def check(self) -> None:
        section = self.service.config_section
        teryt_codes = self._teryt_codes()
        area_label = self.bot.config.get(
            section, "imgw_meteo_area_label", fallback="Bialystok / powiat bialostocki"
        )

        loop = asyncio.get_event_loop()
        try:
            records = await loop.run_in_executor(
                None, alert_sources.fetch_imgw_meteo_warnings, teryt_codes, area_label
            )
        except requests.exceptions.RequestException as e:
            self.logger.warning("IMGW warnings request failed: %s", e)
            return
        except (ValueError, KeyError) as e:
            self.logger.warning("IMGW warnings response parse error: %s", e)
            return

        send_details = self.bot.config.getboolean(section, "imgw_meteo_send_details", fallback=False)
        max_alerts_per_hour = self.bot.config.getint(section, "imgw_meteo_max_alerts_per_hour", fallback=12)
        max_age_hours = self.bot.config.getint(section, "imgw_meteo_max_age_hours", fallback=24)

        await self._process_new_alerts(
            records, self._format_message, send_details, max_alerts_per_hour, max_age_hours
        )

    def _format_message(self, record: dict[str, Any]) -> str:
        prefix = self._translate("services.alerts.imgw_meteo.prefix")
        title = record.get("title") or "?"
        severity = record.get("severity") or "?"
        date = self._format_date_short(record.get("published_at"))
        until = self._format_dt_short(record.get("valid_to"))
        date_part = f" [{date}]" if date else ""
        until_part = ""
        if until:
            until_phrase = self._translate("services.alerts.until", time=until)
            until_part = f" — {until_phrase}"
        return self.render_template(
            _TEMPLATES, "compact",
            prefix=prefix, title=title, severity=severity, date=date or "", until=until or "",
            date_part=date_part, until_part=until_part,
            description=record.get("description") or "",
            area=record.get("area") or "",
        )
