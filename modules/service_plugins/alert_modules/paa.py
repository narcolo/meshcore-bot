"""PAA radiation dose-rate readings for configured stations -- a *scheduled
digest*: fetches all configured stations and pushes one combined message at
fixed times of day (default 06:00/18:00), always, regardless of readings. Not
an interval, and not threshold-gated (see base.py's schedule_kind="daily").
"""

from __future__ import annotations

import asyncio
import math
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


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


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
            readings = await loop.run_in_executor(None, alert_sources.fetch_paa_all_readings, bbox)
        except requests.exceptions.RequestException as e:
            self.logger.warning("PAA request failed: %s", e)
            return
        except (ValueError, KeyError) as e:
            self.logger.warning("PAA response parse error: %s", e)
            return

        section = self.service.config_section
        max_reading_age_hours = self.bot.config.getfloat(section, "paa_max_reading_age_hours", fallback=6.0)
        fallback_max_km = self.bot.config.getfloat(section, "paa_fallback_max_km", fallback=50.0)

        by_normalized = {alert_sources.normalize_station_name(r["station"]): r for r in readings}
        fresh = [r for r in readings if not self._is_stale_reading(r, max_reading_age_hours)]
        resolved: dict[str, Optional[dict[str, Any]]] = {}
        for configured in stations:
            own = by_normalized.get(alert_sources.normalize_station_name(configured))
            if own is not None and not self._is_stale_reading(own, max_reading_age_hours):
                resolved[configured] = own
                continue
            if own is not None:
                self.logger.info(
                    "PAA station %s reading (%s) is stale", own["station"], own.get("timestamp"),
                )
            backup = self._nearest_fresh(own, fresh, fallback_max_km) if own is not None else None
            if backup is not None:
                reading, distance_km = backup
                self.logger.info(
                    "PAA station %s has no fresh reading; using %s (%.0f km away)",
                    own["station"], reading["station"], distance_km,
                )
                resolved[configured] = dict(
                    reading, fallback_for=own["station"], distance_km=distance_km
                )
                continue
            self.logger.warning("PAA station %r has no fresh reading for this digest", configured)
            resolved[configured] = None

        text = self._format_digest(resolved)
        await self._send_simple_message(text)

    @staticmethod
    def _nearest_fresh(
        own: dict[str, Any], fresh: list[dict[str, Any]], max_km: float
    ) -> Optional[tuple[dict[str, Any], float]]:
        """The nearest fresh reading within max_km of `own`'s coordinates, with its
        distance. None when disabled (max_km <= 0), `own` has no coordinates, or
        nothing fresh is in range."""
        if max_km <= 0 or own.get("lat") is None or own.get("lon") is None:
            return None
        best: Optional[tuple[dict[str, Any], float]] = None
        for candidate in fresh:
            if candidate.get("lat") is None or candidate.get("lon") is None:
                continue
            distance = _distance_km(own["lat"], own["lon"], candidate["lat"], candidate["lon"])
            if distance <= max_km and (best is None or distance < best[1]):
                best = (candidate, distance)
        return best

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
        station without a fresh reading is replaced by the nearest fresh station
        within paa_fallback_max_km (labelled), or shows as 'no data' when there
        is none -- never silently dropped, so a broken/lagging station stays visible."""
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
            if reading.get("fallback_for"):
                # A substitute is labelled with the station it really came from
                # and how far away it is, so it is never mistaken for the city's own.
                parts.append(
                    f"{reading['fallback_for']}: {reading['value']}{marker} "
                    f"({reading['station']}, {reading['distance_km']:.0f} km)"
                )
            else:
                parts.append(f"{reading['station']}: {reading['value']}{marker}")
            unit = reading.get("unit") or unit
        when = datetime.now().strftime("%H:%M %d.%m")
        return self.render_template(
            _TEMPLATES, "compact", prefix=prefix, when=when, stations=", ".join(parts), unit=unit,
        )
