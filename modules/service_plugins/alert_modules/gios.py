"""GIOS overall air quality index for a configured station -- a continuous
*measurement* source (see threshold_source.py): alerts on threshold crossing
with hysteresis instead of "new item" dedup, polled on a fixed interval.
"""

from __future__ import annotations

import asyncio
from typing import Any

import requests

from ...clients import alert_sources
from .threshold_source import ThresholdAlertSourceBase

# Polish 6-level AQI scale, worst to best is the opposite of this list -- higher
# rank = worse air. "Brak indeksu" (no index computed) is deliberately absent:
# alert_sources.fetch_gios_aqindex() already returns None for it, treated as
# "unknown" upstream of this ranking, never as a rank to compare against.
GIOS_CATEGORY_RANK = {
    "Bardzo dobry": 0,
    "Dobry": 1,
    "Umiarkowany": 2,
    "Dostateczny": 3,
    "Zły": 4,
    "Bardzo zły": 5,
}

# compact = current (pre-template-system) behavior, byte-for-byte. date_part is
# pre-built by _format_message (empty string when absent).
_TEMPLATES = {
    "compact": "{icon} {prefix}: {category}{date_part}",
    "minimal": "{icon} {category}",
}


class GiosSource(ThresholdAlertSourceBase):
    name = "gios"

    def is_enabled(self) -> bool:
        return self.bot.config.getboolean(self.service.config_section, "gios_enabled", fallback=False)

    def poll_interval(self) -> float:
        return self.bot.config.getint(self.service.config_section, "gios_poll_interval", fallback=900)

    async def check(self) -> None:
        section = self.service.config_section
        station_id = self.bot.config.getint(section, "gios_station_id", fallback=11174)

        loop = asyncio.get_event_loop()
        try:
            reading = await loop.run_in_executor(None, alert_sources.fetch_gios_aqindex, station_id)
        except requests.exceptions.RequestException as e:
            self.logger.warning("GIOS request failed: %s", e)
            return
        except (ValueError, KeyError) as e:
            self.logger.warning("GIOS response parse error: %s", e)
            return

        if reading is None:
            # "Brak indeksu" -- not enough sensors to judge. Leave existing
            # threshold state untouched rather than guessing either way.
            self.logger.debug("GIOS station %s has no computable index this poll", station_id)
            return

        alert_category = self.bot.config.get(section, "gios_alert_category", fallback="Zły").strip()
        renotify_hours = self.bot.config.getfloat(section, "gios_renotify_hours", fallback=6.0)

        category = reading["category"]
        threshold_rank = GIOS_CATEGORY_RANK.get(alert_category, GIOS_CATEGORY_RANK["Zły"])
        category_rank = GIOS_CATEGORY_RANK.get(category)
        if category_rank is None:
            self.logger.warning("GIOS returned an unrecognized category %r, skipping", category)
            return
        elevated = category_rank >= threshold_rank

        station_key = str(station_id)
        text = await self._apply_threshold_state(
            station_key=station_key,
            elevated=elevated,
            build_alert_text=lambda: self._format_message(reading, elevated=True),
            build_normal_text=lambda: self._format_message(reading, elevated=False),
            renotify_hours=renotify_hours,
        )
        if text:
            await self._send_simple_message(text)
            self._persist_threshold_state()

    def _format_message(self, reading: dict[str, Any], elevated: bool) -> str:
        key = "services.alerts.gios.prefix" if elevated else "services.alerts.gios.normal_prefix"
        prefix = self._translate(key)
        icon = "🌫️" if elevated else "✅"
        date = self._format_date_short(reading.get("computed_at"))
        date_part = f" [{date}]" if date else ""
        return self.render_template(
            _TEMPLATES, "compact",
            icon=icon, prefix=prefix, category=reading["category"], date=date or "", date_part=date_part,
        )
