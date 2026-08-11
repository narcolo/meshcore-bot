"""PAA radiation dose-rate readings for configured stations -- a *scheduled
digest*: fetches all configured stations and pushes one combined message at
fixed times of day (default 06:00/18:00), always, regardless of readings. Not
an interval, and not threshold-gated (see base.py's schedule_kind="daily").
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time
from typing import Any, Optional

import requests

from ...clients import alert_sources
from .base import AlertSourceBase

# compact = current (pre-template-system) behavior, byte-for-byte. `stations` is
# a single pre-joined "Name: value(marker), Name2: value2" list -- not
# independently templated per station, to avoid two-level template composition
# for one digest.
_TEMPLATES = {
    "compact": "☢️ {prefix} [{when}]: {stations} {unit}",
}


class PaaSource(AlertSourceBase):
    name = "paa"
    schedule_kind = "daily"

    def is_enabled(self) -> bool:
        return self.bot.config.getboolean(self.service.config_section, "paa_enabled", fallback=False)

    def schedule_times(self) -> list[time]:
        raw = self.bot.config.get(self.service.config_section, "paa_schedule_times", fallback="06:00,18:00")
        return self._parse_schedule_times(raw)

    def _parse_schedule_times(self, raw: str) -> list[time]:
        """'06:00,18:00' -> [time(6,0), time(18,0)]. Invalid entries are logged
        and skipped; falls back to 06:00/18:00 if nothing valid remains."""
        parsed = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                parsed.append(datetime.strptime(part, "%H:%M").time())
            except ValueError:
                self.logger.warning("Ignoring invalid PAA schedule time %r (expected HH:MM)", part)
        return sorted(set(parsed)) or [time(6, 0), time(18, 0)]

    def _stations(self) -> list[str]:
        raw = self.bot.config.get(
            self.service.config_section, "paa_stations", fallback="Bialystok,Suwalki,Siemiatycze"
        )
        return [s.strip() for s in raw.split(",") if s.strip()]

    async def check(self) -> None:
        stations = self._stations()
        bbox = self.bot.config.get(
            self.service.config_section, "paa_bbox", fallback=alert_sources.PAA_PODLASKIE_BBOX
        )

        loop = asyncio.get_event_loop()
        try:
            readings = await loop.run_in_executor(None, alert_sources.fetch_paa_radiation, stations, bbox)
        except requests.exceptions.RequestException as e:
            self.logger.warning("PAA request failed: %s", e)
            return
        except (ValueError, KeyError) as e:
            self.logger.warning("PAA response parse error: %s", e)
            return

        section = self.service.config_section
        max_reading_age_hours = self.bot.config.getfloat(section, "paa_max_reading_age_hours", fallback=6.0)

        by_normalized = {alert_sources.normalize_station_name(r["station"]): r for r in readings}
        resolved: dict[str, Optional[dict[str, Any]]] = {}
        for configured in stations:
            reading = by_normalized.get(alert_sources.normalize_station_name(configured))
            if reading is not None and self._is_stale_reading(reading, max_reading_age_hours):
                self.logger.info(
                    "PAA station %s reading (%s) is stale, showing as no-data in the digest",
                    reading["station"], reading.get("timestamp"),
                )
                reading = None
            if reading is None:
                self.logger.warning("PAA station %r has no fresh reading for this digest", configured)
            resolved[configured] = reading

        text = self._format_digest(resolved)
        await self._send_simple_message(text)

    def _is_stale_reading(self, reading: dict[str, Any], max_reading_age_hours: float) -> bool:
        """Fail open (not stale) when the timestamp is missing/unparseable, same
        rationale as EventAlertSourceBase._is_stale -- staleness is a safety
        filter, not the primary signal."""
        measured = self._parse_dt(reading.get("timestamp"))
        if measured is None:
            return False
        age_hours = (datetime.now() - measured).total_seconds() / 3600
        return age_hours > max_reading_age_hours

    def _format_digest(self, resolved: dict[str, Optional[dict[str, Any]]]) -> str:
        """One combined message, stations in paa_stations' configured order. A
        station without a fresh reading shows as 'no data' rather than being
        silently dropped, so a broken/lagging station stays visible."""
        section = self.service.config_section
        alert_dose_rate_usvh = self.bot.config.getfloat(section, "paa_alert_dose_rate_usvh", fallback=0.3)

        prefix = self._translate("services.alerts.paa.digest_prefix")
        no_data = self._translate("services.alerts.paa.no_data")
        unit = "µSv/h"
        parts = []
        for configured, reading in resolved.items():
            if reading is None:
                parts.append(f"{configured}: {no_data}")
                continue
            marker = "⚠️" if reading["value"] >= alert_dose_rate_usvh else ""
            parts.append(f"{reading['station']}: {reading['value']}{marker}")
            unit = reading.get("unit") or unit
        when = datetime.now().strftime("%H:%M %d.%m")
        return self.render_template(
            _TEMPLATES, "compact", prefix=prefix, when=when, stations=", ".join(parts), unit=unit,
        )
