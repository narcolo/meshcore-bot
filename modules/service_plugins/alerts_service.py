#!/usr/bin/env python3
"""
Alerts Service for MeshCore Bot (Phase 2a + 2b)

Background push service — polls official Bialystok/Podlaskie alert sources on a
configurable interval and automatically posts new alerts to a MeshCore channel.
Not a user command: nobody has to ask for this, it fires on its own.

Sources (see plans/alert-feeds-research.md and plans/phase2-alert-integration-plan.md):
- IMGW meteorological warnings, filtered to warnings touching Bialystok/powiat bialostocki
- RSO (Regionalny System Ostrzegania) for wojewodztwo podlaskie, which also carries
  republished Alert RCB messages
- GIOS air quality index for a configured station (threshold-crossing alert, Phase 2b)
- PAA radiation dose-rate readings for configured stations (fixed-time daily digest,
  Phase 2b)

Three different push shapes live in this one service:
- IMGW/RSO are discrete *events* with stable ids -- "is this new?" dedup (see
  _process_new_alerts), polled on a fixed interval.
- GIOS is a continuous *measurement* with no natural "new item" -- it alerts on
  threshold crossing instead, with hysteresis so it doesn't re-fire every poll while
  sitting above the line (see _apply_threshold_state), also on a fixed interval.
- PAA is a *scheduled digest*: fetches all configured stations and pushes one combined
  message at fixed times of day (default 06:00/18:00), always, regardless of readings
  (see _daily_schedule_loop) -- not an interval, and not threshold-gated.

Poll loops run one per source, all independent, so a slow or failing source never
delays or blocks the others -- IMGW/RSO's loop is a near-direct port of
EarthquakeService's poll-loop/dedup/send pattern
(modules/service_plugins/earthquake_service.py).
"""

import asyncio
import contextlib
import json
from collections import deque
from datetime import datetime, time, timedelta
from typing import Any, Callable, Coroutine, Optional

import requests

from ..clients import alert_sources
from .base_service import BaseServicePlugin

# Per-chunk budget (UTF-8 bytes) for splitting a long alert into several messages
# rather than truncating it -- same conservative ballpark CLAUDE.md documents for
# channel sends (~148 chars after the bot's username prefix). Byte-based (not a
# character count) since split_text_into_utf8_chunks is codepoint-safe and Polish
# diacritics/emoji are multi-byte in UTF-8.
MESSAGE_CHUNK_MAX_BYTES = 140

# Per-source dedup state (bot_metadata, JSON-encoded list of external_ids, most
# recent last) is capped at this many entries so it never grows unbounded — the
# practical "watermark" the phase 2 plan describes, just id-set-shaped rather than
# timestamp-shaped since both sources already hand back a small, already-filtered
# "currently active" list with stable ids (no need for USGS-style time windowing).
SEEN_IDS_MAX = 200

METADATA_KEY_IMGW_SEEN = "alerts_imgw_meteo_seen_ids"
METADATA_KEY_RSO_SEEN = "alerts_rso_seen_ids"

# Threshold-crossing state for GIOS (bot_metadata, JSON dict) -- see
# _apply_threshold_state. Keyed by station (GIOS only has the one configured
# station, but stored dict-shaped for consistency/future multi-station support).
METADATA_KEY_GIOS_STATE = "alerts_gios_threshold_state"

# Last daily-schedule slot PAA successfully fired for (bot_metadata, plain ISO
# datetime string) -- see _daily_schedule_loop. Restart-safe: a bot restart within
# the same slot must not re-send the digest.
METADATA_KEY_PAA_LAST_SLOT = "alerts_paa_last_slot"

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


class AlertsService(BaseServicePlugin):
    """Polls IMGW + RSO and pushes new alerts to a MeshCore channel automatically."""

    config_section = "Alerts_Service"
    description = "Automatic push alerts for Bialystok/Podlaskie (IMGW warnings + RSO)"

    # Web-viewer settings schema (see modules/settings_schema.py)
    settings_schema = [
        {"key": "channel", "label": "Channel", "type": "str", "default": "general",
         "help": "Channel name to post alerts to (e.g. #alerts).", "required": True},
        {"key": "flood_scope", "label": "Flood scope", "type": "str", "default": "",
         "help": "Optional regional TC_FLOOD scope (e.g. pl-podlasie). Empty = use "
                 "[Channels] outgoing_flood_scope_override."},
        {"key": "imgw_meteo_enabled", "label": "IMGW warnings enabled", "type": "bool", "default": True,
         "help": "Poll IMGW meteorological warnings."},
        {"key": "imgw_meteo_poll_interval", "label": "IMGW poll interval", "type": "int",
         "min": 30, "default": 300, "unit": "s", "help": "How often to poll IMGW."},
        {"key": "imgw_meteo_send_details", "label": "IMGW send full text", "type": "bool", "default": False,
         "help": "Also send the full warning text as a follow-up message."},
        {"key": "imgw_meteo_max_alerts_per_hour", "label": "IMGW alerts/hour cap", "type": "int",
         "min": 1, "default": 12, "help": "Ceiling on IMGW alerts sent per rolling hour."},
        {"key": "imgw_meteo_max_age_hours", "label": "IMGW max alert age", "type": "int",
         "min": 1, "default": 24, "unit": "h",
         "help": "Skip warnings published longer ago than this (stale-dedup-reset guard)."},
        {"key": "rso_enabled", "label": "RSO enabled", "type": "bool", "default": True,
         "help": "Poll RSO (Regionalny System Ostrzegania) for wojewodztwo podlaskie."},
        {"key": "rso_poll_interval", "label": "RSO poll interval", "type": "int",
         "min": 30, "default": 300, "unit": "s", "help": "How often to poll RSO."},
        {"key": "rso_send_details", "label": "RSO send full text", "type": "bool", "default": False,
         "help": "Also send the full alert text as a follow-up message."},
        {"key": "rso_max_alerts_per_hour", "label": "RSO alerts/hour cap", "type": "int",
         "min": 1, "default": 12, "help": "Ceiling on RSO alerts sent per rolling hour."},
        {"key": "rso_max_age_hours", "label": "RSO max alert age", "type": "int",
         "min": 1, "default": 24, "unit": "h",
         "help": "Skip alerts published longer ago than this (stale-dedup-reset guard)."},
        {"key": "gios_enabled", "label": "GIOS air quality enabled", "type": "bool", "default": False,
         "help": "Poll GIOS overall air quality index for gios_station_id."},
        {"key": "gios_poll_interval", "label": "GIOS poll interval", "type": "int",
         "min": 60, "default": 900, "unit": "s", "help": "How often to poll GIOS."},
        {"key": "gios_station_id", "label": "GIOS station ID", "type": "int", "default": 11174,
         "help": "GIOS station to monitor (11174 = ul. 42 Pulku Piechoty, Bialystok -- "
                 "the only Bialystok station with enough sensors for an overall index)."},
        {"key": "gios_alert_category", "label": "GIOS alert threshold", "type": "str",
         "default": "Zły",
         "help": "Alert when the AQI category reaches or exceeds this (Polish 6-level "
                 "scale, worst to best: Bardzo zły, Zły, Dostateczny, Umiarkowany, "
                 "Dobry, Bardzo dobry)."},
        {"key": "gios_renotify_hours", "label": "GIOS re-notify interval", "type": "int",
         "min": 1, "default": 6, "unit": "h",
         "help": "Minimum hours between repeat reminders while still above threshold."},
        {"key": "paa_enabled", "label": "PAA radiation monitoring enabled", "type": "bool", "default": False,
         "help": "Push a combined PAA radiation digest for paa_stations at paa_schedule_times."},
        {"key": "paa_schedule_times", "label": "PAA schedule (HH:MM,HH:MM)", "type": "str",
         "default": "06:00,18:00",
         "help": "Comma-separated daily times (24h HH:MM, local time) to push the digest. "
                 "Always sent at each time, regardless of readings -- not threshold-gated."},
        {"key": "paa_stations", "label": "PAA stations", "type": "str",
         "default": "Bialystok,Suwalki,Siemiatycze",
         "help": "Comma-separated PAA station names to include in the digest, in this "
                 "order (plain ASCII is fine -- matching is diacritic/case-insensitive)."},
        {"key": "paa_alert_dose_rate_usvh", "label": "PAA warning marker threshold", "type": "float",
         "default": 0.3, "unit": "uSv/h",
         "help": "Purely cosmetic: a station at or above this gets a warning marker in the "
                 "digest. NOT an official PAA public-alert standard -- no such published "
                 "threshold was found in research. A placeholder several times above the "
                 "~0.06-0.08 uSv/h background observed at Podlaskie stations; retune from "
                 "real readings, don't treat as authoritative."},
        {"key": "paa_max_reading_age_hours", "label": "PAA max reading age", "type": "int",
         "min": 1, "default": 6, "unit": "h",
         "help": "Show a station as 'no data' in the digest rather than an actually-stale "
                 "reading (some PAA stations have been observed days stale)."},
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)

        section = self.config_section
        self.channel = self.bot.config.get(section, "channel", fallback="general")

        self.imgw_enabled = self.bot.config.getboolean(section, "imgw_meteo_enabled", fallback=True)
        self.imgw_poll_interval = self.bot.config.getint(section, "imgw_meteo_poll_interval", fallback=300)
        self.imgw_send_details = self.bot.config.getboolean(section, "imgw_meteo_send_details", fallback=False)
        self.imgw_max_alerts_per_hour = self.bot.config.getint(
            section, "imgw_meteo_max_alerts_per_hour", fallback=12
        )
        self.imgw_max_age_hours = self.bot.config.getint(
            section, "imgw_meteo_max_age_hours", fallback=24
        )

        self.rso_enabled = self.bot.config.getboolean(section, "rso_enabled", fallback=True)
        self.rso_poll_interval = self.bot.config.getint(section, "rso_poll_interval", fallback=300)
        self.rso_send_details = self.bot.config.getboolean(section, "rso_send_details", fallback=False)
        self.rso_max_alerts_per_hour = self.bot.config.getint(
            section, "rso_max_alerts_per_hour", fallback=12
        )
        self.rso_max_age_hours = self.bot.config.getint(
            section, "rso_max_age_hours", fallback=24
        )

        self.gios_enabled = self.bot.config.getboolean(section, "gios_enabled", fallback=False)
        self.gios_poll_interval = self.bot.config.getint(section, "gios_poll_interval", fallback=900)
        self.gios_station_id = self.bot.config.getint(section, "gios_station_id", fallback=11174)
        self.gios_alert_category = self.bot.config.get(
            section, "gios_alert_category", fallback="Zły"
        ).strip()
        self.gios_renotify_hours = self.bot.config.getfloat(
            section, "gios_renotify_hours", fallback=6.0
        )

        self.paa_enabled = self.bot.config.getboolean(section, "paa_enabled", fallback=False)
        self.paa_schedule_times = self._parse_schedule_times(
            self.bot.config.get(section, "paa_schedule_times", fallback="06:00,18:00")
        )
        self.paa_stations = [
            s.strip() for s in
            self.bot.config.get(section, "paa_stations", fallback="Bialystok,Suwalki,Siemiatycze").split(",")
            if s.strip()
        ]
        self.paa_alert_dose_rate_usvh = self.bot.config.getfloat(
            section, "paa_alert_dose_rate_usvh", fallback=0.3
        )
        self.paa_max_reading_age_hours = self.bot.config.getfloat(
            section, "paa_max_reading_age_hours", fallback=6.0
        )

        self._tasks: list[asyncio.Task] = []
        self._seen_ids: dict[str, set[str]] = {
            "imgw_meteo": self._load_seen_ids(METADATA_KEY_IMGW_SEEN),
            "rso": self._load_seen_ids(METADATA_KEY_RSO_SEEN),
        }
        # Insertion order, so we can evict the oldest when trimming to SEEN_IDS_MAX.
        self._seen_order: dict[str, deque] = {
            "imgw_meteo": deque(self._seen_ids["imgw_meteo"], maxlen=SEEN_IDS_MAX),
            "rso": deque(self._seen_ids["rso"], maxlen=SEEN_IDS_MAX),
        }
        # Rolling one-hour send timestamps per source, for the alerts/hour cap.
        self._sent_timestamps: dict[str, deque] = {"imgw_meteo": deque(), "rso": deque()}

        # Threshold-crossing state (GIOS only): station -> {"state": "normal"|"elevated",
        # "last_notified_at": iso str | None}.
        self._gios_state: dict[str, dict[str, Any]] = self._load_threshold_state(METADATA_KEY_GIOS_STATE)

        self.logger.info(
            "Alerts service initialized: channel=%s, imgw=%s (every %ss), rso=%s (every %ss), "
            "gios=%s (every %ss), paa=%s (at %s)",
            self.channel,
            self.imgw_enabled,
            self.imgw_poll_interval,
            self.rso_enabled,
            self.rso_poll_interval,
            self.gios_enabled,
            self.gios_poll_interval,
            self.paa_enabled,
            ",".join(t.strftime("%H:%M") for t in self.paa_schedule_times),
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if not self.enabled:
            self.logger.info("Alerts service is disabled, not starting")
            return
        self._running = True
        self.logger.info("Starting alerts service")

        if self.imgw_enabled:
            self._tasks.append(
                asyncio.create_task(
                    self._poll_loop("imgw_meteo", self._check_imgw, self.imgw_poll_interval)
                )
            )
        if self.rso_enabled:
            self._tasks.append(
                asyncio.create_task(
                    self._poll_loop("rso", self._check_rso, self.rso_poll_interval)
                )
            )
        if self.gios_enabled:
            self._tasks.append(
                asyncio.create_task(
                    self._poll_loop("gios", self._check_gios, self.gios_poll_interval)
                )
            )
        if self.paa_enabled:
            self._tasks.append(
                asyncio.create_task(
                    self._daily_schedule_loop(
                        "paa", self._check_paa, self.paa_schedule_times, METADATA_KEY_PAA_LAST_SLOT
                    )
                )
            )

        if not self._tasks:
            self.logger.info("Alerts service started with no sources enabled (nothing to poll)")
        else:
            self.logger.info("Alerts service started (%d source(s) polling)", len(self._tasks))

    async def stop(self) -> None:
        self._running = False
        self.logger.info("Stopping alerts service")
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        self.logger.info("Alerts service stopped")

    # ------------------------------------------------------------------
    # Poll loop (interval-based; IMGW/RSO/GIOS each run their own instance as
    # an independent task, so one source's failure/timeout never delays or
    # blocks the others)
    # ------------------------------------------------------------------

    async def _poll_loop(
        self,
        name: str,
        check_fn: Callable[[], Coroutine[Any, Any, None]],
        interval: float,
    ) -> None:
        self.logger.info("Alerts poll loop started: %s (interval=%ss)", name, interval)
        while self._running:
            try:
                await check_fn()
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Error in alerts poll loop (%s): %s", name, e)
                await asyncio.sleep(60)

    # ------------------------------------------------------------------
    # Daily-schedule loop (fixed times of day, not an interval; PAA uses this).
    #
    # A plain "sleep N seconds" interval doesn't give fixed wall-clock times --
    # it fires check_fn() immediately on every start() (see _poll_loop above),
    # so a bot that restarts a few times a day would fire far more than
    # intended, and even without restarts the actual times drift with whenever
    # the bot happened to start rather than landing at e.g. 06:00/18:00. This
    # loop instead computes the most recent daily slot that's already passed,
    # fires at most once per slot (persisted to bot_metadata so a restart
    # inside the same slot doesn't re-fire), then sleeps exactly until the next
    # slot boundary.
    # ------------------------------------------------------------------

    async def _daily_schedule_loop(
        self,
        name: str,
        check_fn: Callable[[], Coroutine[Any, Any, None]],
        times: list[time],
        metadata_key: str,
    ) -> None:
        self.logger.info(
            "Alerts daily-schedule loop started: %s (times=%s)",
            name, ",".join(t.strftime("%H:%M") for t in times),
        )
        while self._running:
            try:
                now = datetime.now()
                today_slots = sorted(datetime.combine(now.date(), t) for t in times)
                past_slots = [s for s in today_slots if s <= now]

                if past_slots:
                    candidate = past_slots[-1]
                    if self._load_last_slot(metadata_key) != candidate.isoformat():
                        await check_fn()
                        self._persist_last_slot(metadata_key, candidate.isoformat())

                future_slots = [s for s in today_slots if s > now]
                next_slot = future_slots[0] if future_slots else today_slots[0] + timedelta(days=1)
                await asyncio.sleep(max((next_slot - datetime.now()).total_seconds(), 1.0))
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Error in alerts daily-schedule loop (%s): %s", name, e)
                await asyncio.sleep(60)

    def _parse_schedule_times(self, raw: str) -> list[time]:
        """'06:00,18:00' -> [time(6,0), time(18,0)]. Invalid entries are logged and
        skipped; falls back to 06:00/18:00 if nothing valid remains."""
        parsed = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                parsed.append(datetime.strptime(part, "%H:%M").time())
            except ValueError:
                self.logger.warning("Ignoring invalid schedule time %r (expected HH:MM)", part)
        return sorted(set(parsed)) or [time(6, 0), time(18, 0)]

    def _load_last_slot(self, metadata_key: str) -> Optional[str]:
        db_manager = getattr(self.bot, "db_manager", None)
        if not db_manager:
            return None
        return db_manager.get_metadata(metadata_key)

    def _persist_last_slot(self, metadata_key: str, value: str) -> None:
        db_manager = getattr(self.bot, "db_manager", None)
        if not db_manager:
            return
        db_manager.set_metadata(metadata_key, value)

    # ------------------------------------------------------------------
    # IMGW
    # ------------------------------------------------------------------

    async def _check_imgw(self) -> None:
        loop = asyncio.get_event_loop()
        try:
            records = await loop.run_in_executor(None, alert_sources.fetch_imgw_meteo_warnings)
        except requests.exceptions.RequestException as e:
            self.logger.warning("IMGW warnings request failed: %s", e)
            return
        except (ValueError, KeyError) as e:
            self.logger.warning("IMGW warnings response parse error: %s", e)
            return

        await self._process_new_alerts(
            source="imgw_meteo",
            records=records,
            formatter=self._format_imgw_message,
            send_details=self.imgw_send_details,
            max_alerts_per_hour=self.imgw_max_alerts_per_hour,
            max_age_hours=self.imgw_max_age_hours,
        )

    def _format_imgw_message(self, record: dict[str, Any]) -> str:
        """Compact header only -- the full warning text (tresc) is sent separately
        via send_details, since it's usually much longer than the header. Returned
        untruncated; _send_alert chunks it if a very long title ever exceeds one
        message rather than cutting it off."""
        prefix = self._translate("services.alerts.imgw_meteo.prefix")
        title = record.get("title") or "?"
        severity = record.get("severity") or "?"
        date = self._format_date_short(record.get("published_at"))
        valid_to = self._format_dt_short(record.get("valid_to"))
        text = f"⚠️ {prefix}: {title} (st.{severity})"
        if date:
            text = f"{text} [{date}]"
        if valid_to:
            until = self._translate("services.alerts.until", time=valid_to)
            text = f"{text} — {until}"
        return text

    # ------------------------------------------------------------------
    # RSO
    # ------------------------------------------------------------------

    async def _check_rso(self) -> None:
        loop = asyncio.get_event_loop()
        try:
            records = await loop.run_in_executor(None, alert_sources.fetch_rso_podlaskie)
        except requests.exceptions.RequestException as e:
            self.logger.warning("RSO request failed: %s", e)
            return
        except (ValueError, KeyError) as e:
            self.logger.warning("RSO response parse error: %s", e)
            return

        await self._process_new_alerts(
            source="rso",
            records=records,
            formatter=self._format_rso_message,
            send_details=self.rso_send_details,
            max_alerts_per_hour=self.rso_max_alerts_per_hour,
            max_age_hours=self.rso_max_age_hours,
        )

    def _format_rso_message(self, record: dict[str, Any]) -> str:
        """Header + the full description merged into one text -- unlike IMGW, RSO's
        description is normally short enough to matter for the default message.
        Returned untruncated; _send_alert splits it into multiple messages instead
        of cutting it off when it doesn't fit in one."""
        prefix = self._translate("services.alerts.rso.prefix")
        title = record.get("title") or "?"
        description = record.get("description") or ""
        date = self._format_date_short(record.get("published_at"))
        base = f"📢 {prefix}"
        if date:
            base = f"{base} [{date}]"
        base = f"{base}: {title}"
        if description and description != title:
            base = f"{base} — {description}"
        return base

    # ------------------------------------------------------------------
    # GIOS air quality (Phase 2b — threshold-crossing, not event dedup)
    # ------------------------------------------------------------------

    async def _check_gios(self) -> None:
        loop = asyncio.get_event_loop()
        try:
            reading = await loop.run_in_executor(
                None, alert_sources.fetch_gios_aqindex, self.gios_station_id
            )
        except requests.exceptions.RequestException as e:
            self.logger.warning("GIOS request failed: %s", e)
            return
        except (ValueError, KeyError) as e:
            self.logger.warning("GIOS response parse error: %s", e)
            return

        if reading is None:
            # "Brak indeksu" -- not enough sensors to judge. Leave existing
            # threshold state untouched rather than guessing either way.
            self.logger.debug(
                "GIOS station %s has no computable index this poll", self.gios_station_id
            )
            return

        category = reading["category"]
        threshold_rank = GIOS_CATEGORY_RANK.get(self.gios_alert_category, GIOS_CATEGORY_RANK["Zły"])
        category_rank = GIOS_CATEGORY_RANK.get(category)
        if category_rank is None:
            self.logger.warning("GIOS returned an unrecognized category %r, skipping", category)
            return
        elevated = category_rank >= threshold_rank

        station_key = str(self.gios_station_id)
        text = await self._apply_threshold_state(
            states=self._gios_state,
            station_key=station_key,
            elevated=elevated,
            build_alert_text=lambda: self._format_gios_message(reading, elevated=True),
            build_normal_text=lambda: self._format_gios_message(reading, elevated=False),
            renotify_hours=self.gios_renotify_hours,
        )
        if text:
            await self._send_simple_message(text)
            self._persist_threshold_state(METADATA_KEY_GIOS_STATE, self._gios_state)

    def _format_gios_message(self, reading: dict[str, Any], elevated: bool) -> str:
        key = "services.alerts.gios.prefix" if elevated else "services.alerts.gios.normal_prefix"
        prefix = self._translate(key)
        icon = "🌫️" if elevated else "✅"
        date = self._format_date_short(reading.get("computed_at"))
        text = f"{icon} {prefix}: {reading['category']}"
        if date:
            text = f"{text} [{date}]"
        return text

    # ------------------------------------------------------------------
    # PAA radiation monitoring (Phase 2b — scheduled digest, not threshold-gated:
    # fires unconditionally at each configured daily time, all stations combined
    # into one message; see _daily_schedule_loop)
    # ------------------------------------------------------------------

    async def _check_paa(self) -> None:
        loop = asyncio.get_event_loop()
        try:
            readings = await loop.run_in_executor(
                None, alert_sources.fetch_paa_radiation, self.paa_stations
            )
        except requests.exceptions.RequestException as e:
            self.logger.warning("PAA request failed: %s", e)
            return
        except (ValueError, KeyError) as e:
            self.logger.warning("PAA response parse error: %s", e)
            return

        by_normalized = {alert_sources.normalize_station_name(r["station"]): r for r in readings}
        resolved: dict[str, Optional[dict[str, Any]]] = {}
        for configured in self.paa_stations:
            reading = by_normalized.get(alert_sources.normalize_station_name(configured))
            if reading is not None and self._paa_reading_is_stale(reading):
                self.logger.info(
                    "PAA station %s reading (%s) is stale, showing as no-data in the digest",
                    reading["station"], reading.get("timestamp"),
                )
                reading = None
            if reading is None:
                self.logger.warning("PAA station %r has no fresh reading for this digest", configured)
            resolved[configured] = reading

        text = self._format_paa_digest(resolved)
        await self._send_simple_message(text)

    def _paa_reading_is_stale(self, reading: dict[str, Any]) -> bool:
        """Fail open (not stale) when the timestamp is missing/unparseable, same
        rationale as _is_stale for IMGW/RSO -- staleness is a safety filter, not
        the primary signal."""
        measured = self._parse_dt(reading.get("timestamp"))
        if measured is None:
            return False
        age_hours = (datetime.now() - measured).total_seconds() / 3600
        return age_hours > self.paa_max_reading_age_hours

    def _format_paa_digest(self, resolved: dict[str, Optional[dict[str, Any]]]) -> str:
        """One combined message, stations in paa_stations' configured order. A
        station without a fresh reading shows as 'no data' rather than being
        silently dropped, so a broken/lagging station stays visible."""
        prefix = self._translate("services.alerts.paa.digest_prefix")
        no_data = self._translate("services.alerts.paa.no_data")
        unit = "µSv/h"
        parts = []
        for configured, reading in resolved.items():
            if reading is None:
                parts.append(f"{configured}: {no_data}")
                continue
            marker = "⚠️" if reading["value"] >= self.paa_alert_dose_rate_usvh else ""
            parts.append(f"{reading['station']}: {reading['value']}{marker}")
            unit = reading.get("unit") or unit
        when = datetime.now().strftime("%H:%M %d.%m")
        return f"☢️ {prefix} [{when}]: " + ", ".join(parts) + f" {unit}"

    # ------------------------------------------------------------------
    # Shared: threshold state (GIOS/PAA hysteresis)
    # ------------------------------------------------------------------

    async def _apply_threshold_state(
        self,
        states: dict[str, dict[str, Any]],
        station_key: str,
        elevated: bool,
        build_alert_text: Callable[[], str],
        build_normal_text: Callable[[], str],
        renotify_hours: float,
    ) -> Optional[str]:
        """Hysteresis state machine: only returns text to send on a state
        transition (normal->elevated, elevated->normal), or a "still elevated"
        reminder no more than once per renotify_hours. Returns None when nothing
        should be sent (including the common case: still normal, still fine).
        """
        now = datetime.now()
        entry = states.get(station_key) or {"state": "normal", "last_notified_at": None}
        prev_state = entry["state"]
        text: Optional[str] = None

        if elevated and prev_state == "normal":
            text = build_alert_text()
            entry = {"state": "elevated", "last_notified_at": now.isoformat()}
        elif elevated and prev_state == "elevated":
            due = True
            last = entry.get("last_notified_at")
            if last:
                try:
                    due = (now - datetime.fromisoformat(last)).total_seconds() / 3600 >= renotify_hours
                except ValueError:
                    due = True
            if due:
                text = build_alert_text()
                entry["last_notified_at"] = now.isoformat()
        elif not elevated and prev_state == "elevated":
            text = build_normal_text()
            entry = {"state": "normal", "last_notified_at": None}
        # else: not elevated and prev_state == "normal" -- nothing changed, no-op

        states[station_key] = entry
        return text

    async def _send_simple_message(self, text: str) -> bool:
        """Send a single pre-built text (chunked if it doesn't fit in one message) --
        used by GIOS's threshold alerts and PAA's scheduled digest, neither of which
        need the record/formatter/send_details machinery _send_alert has for
        IMGW/RSO's event dedup path."""
        chunks = self.bot.command_manager.split_text_into_utf8_chunks(text, MESSAGE_CHUNK_MAX_BYTES)
        return await self.bot.command_manager.send_channel_messages_chunked(
            self.channel, chunks, scope=self.get_mesh_flood_scope()
        )

    def _load_threshold_state(self, metadata_key: str) -> dict[str, dict[str, Any]]:
        db_manager = getattr(self.bot, "db_manager", None)
        if not db_manager:
            return {}
        raw = db_manager.get_metadata(metadata_key)
        if not raw:
            return {}
        try:
            state = json.loads(raw)
            if isinstance(state, dict):
                return state
        except (ValueError, TypeError) as e:
            self.logger.warning("Could not parse persisted threshold state for %s: %s", metadata_key, e)
        return {}

    def _persist_threshold_state(self, metadata_key: str, state: dict[str, dict[str, Any]]) -> None:
        db_manager = getattr(self.bot, "db_manager", None)
        if not db_manager:
            return
        db_manager.set_metadata(metadata_key, json.dumps(state))

    # ------------------------------------------------------------------
    # Shared: dedup, rate limiting, sending
    # ------------------------------------------------------------------

    async def _process_new_alerts(
        self,
        source: str,
        records: list[dict[str, Any]],
        formatter: Callable[[dict[str, Any]], str],
        send_details: bool,
        max_alerts_per_hour: int,
        max_age_hours: float,
    ) -> None:
        seen = self._seen_ids[source]
        new_records = [
            r for r in records
            if r.get("external_id") and r.get("external_id") not in seen
        ]
        if not new_records:
            return  # nothing new this poll -- send nothing (requirement: no-op on no new alerts)

        # Drop anything published well before now -- e.g. a dedup-state reset (or a
        # first-ever start against a feed that lists everything currently "active",
        # not just recently-published) must not resurrect week-old news. Still marked
        # seen so a stale item already in the feed isn't re-checked every poll forever.
        fresh_records = []
        stale_count = 0
        for record in new_records:
            if self._is_stale(record, max_age_hours):
                self._mark_seen(source, record["external_id"])
                stale_count += 1
            else:
                fresh_records.append(record)
        if stale_count:
            self.logger.info(
                "%s: skipped %d stale alert(s) (published more than %gh ago)",
                source, stale_count, max_age_hours,
            )
        new_records = fresh_records

        # Oldest first, so a burst of several new alerts posts in chronological order.
        new_records.sort(key=lambda r: r.get("published_at") or "")

        sent_count = 0
        suppressed_count = 0
        failed_count = 0
        for record in new_records:
            external_id = record["external_id"]

            if not self._check_rate_limit(source, max_alerts_per_hour):
                # Hard per-hour ceiling (log-and-drop), not a deferred queue -- mark
                # seen so a suppressed alert is not retried once the cap resets.
                self._mark_seen(source, external_id)
                suppressed_count += 1
                continue

            # No manual pacing needed here: _send_alert goes through
            # send_channel_messages_chunked, which (a) skips the reject-on-limit
            # global rate limiter for automated services -- the thing that silently
            # dropped 10 of 11 alerts before this fix -- and (b) still blocks on the
            # bot's shared TX pacer (bot_tx_rate_limiter) on every send, including
            # across separate calls, so a burst is naturally spaced without any of
            # it being lost.
            try:
                sent = await self._send_alert(record, formatter, send_details)
            except Exception as e:
                self.logger.error(
                    "Error sending %s alert %s: %s", source, external_id, e
                )
                sent = False

            if sent:
                self._mark_seen(source, external_id)
                sent_count += 1
                self._sent_timestamps[source].append(datetime.now().timestamp())
            else:
                # Transient failure (e.g. bot-level rate limit contention with
                # unrelated traffic) -- deliberately NOT marked seen, so this alert
                # is retried on the next poll instead of being silently lost.
                failed_count += 1

        if suppressed_count:
            self.logger.warning(
                "%s: %d alert(s) suppressed this poll (over %d/hour cap)",
                source, suppressed_count, max_alerts_per_hour,
            )
        if failed_count:
            self.logger.warning(
                "%s: %d alert(s) failed to send this poll, will retry next poll",
                source, failed_count,
            )
        if sent_count:
            self.logger.info("%s: sent %d new alert(s)", source, sent_count)

        self._persist_seen_ids(source)

    async def _send_alert(
        self,
        record: dict[str, Any],
        formatter: Callable[[dict[str, Any]], str],
        send_details: bool,
    ) -> bool:
        """Returns True only if every chunk of the primary message actually sent.

        Long text is split into several messages (split_text_into_utf8_chunks +
        send_channel_messages_chunked -- the same word/codepoint-safe chunking and
        TX-rate-limit-paced multi-send the rest of the bot already uses) rather
        than truncated, so nothing is silently cut off mid-sentence.
        """
        text = formatter(record)
        chunks = self.bot.command_manager.split_text_into_utf8_chunks(
            text, MESSAGE_CHUNK_MAX_BYTES
        )
        sent = await self.bot.command_manager.send_channel_messages_chunked(
            self.channel, chunks, scope=self.get_mesh_flood_scope()
        )
        if not sent:
            return False

        if send_details:
            description = record.get("description")
            # Skip when the formatter already merged the description into `text`
            # (RSO) -- only IMGW's header omits it, so only IMGW gets a real
            # follow-up here; sending it again for RSO would just duplicate.
            if description and description not in text:
                detail_chunks = self.bot.command_manager.split_text_into_utf8_chunks(
                    description, MESSAGE_CHUNK_MAX_BYTES
                )
                await self.bot.command_manager.send_channel_messages_chunked(
                    self.channel, detail_chunks, scope=self.get_mesh_flood_scope()
                )
        return True

    def _check_rate_limit(self, source: str, max_per_hour: int) -> bool:
        """True if another alert may be sent for this source right now."""
        now = datetime.now().timestamp()
        timestamps = self._sent_timestamps[source]
        while timestamps and now - timestamps[0] > 3600:
            timestamps.popleft()
        return len(timestamps) < max_per_hour

    # ------------------------------------------------------------------
    # Dedup persistence (bot_metadata; no new table)
    # ------------------------------------------------------------------

    def _load_seen_ids(self, metadata_key: str) -> set[str]:
        db_manager = getattr(self.bot, "db_manager", None)
        if not db_manager:
            return set()
        raw = db_manager.get_metadata(metadata_key)
        if not raw:
            return set()
        try:
            ids = json.loads(raw)
            if isinstance(ids, list):
                return {str(i) for i in ids}
        except (ValueError, TypeError) as e:
            self.logger.warning("Could not parse persisted alert ids for %s: %s", metadata_key, e)
        return set()

    def _mark_seen(self, source: str, external_id: str) -> None:
        external_id = str(external_id)
        if external_id in self._seen_ids[source]:
            return
        order = self._seen_order[source]
        if len(order) == order.maxlen:
            evicted = order.popleft()
            self._seen_ids[source].discard(evicted)
        order.append(external_id)
        self._seen_ids[source].add(external_id)

    def _persist_seen_ids(self, source: str) -> None:
        db_manager = getattr(self.bot, "db_manager", None)
        if not db_manager:
            return
        metadata_key = METADATA_KEY_IMGW_SEEN if source == "imgw_meteo" else METADATA_KEY_RSO_SEEN
        db_manager.set_metadata(metadata_key, json.dumps(list(self._seen_order[source])))

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    def _translate(self, key: str, **kwargs) -> str:
        translator = getattr(self.bot, "translator", None)
        if translator is None:
            return key
        return translator.translate(key, **kwargs)

    # IMGW/RSO timestamps carry seconds ("...:00"); PAA's don't ("HH:MM" only) --
    # tried in this order so the common (with-seconds) case is the fast path.
    _DT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")

    @classmethod
    def _parse_dt(cls, value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        for fmt in cls._DT_FORMATS:
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

    def _is_stale(self, record: dict[str, Any], max_age_hours: float) -> bool:
        """True if the record's published date is older than max_age_hours.

        Fails open (not stale) when no usable timestamp is present, so a record
        missing/with an unparseable date is never silently suppressed -- staleness
        is an extra safety filter, not the primary dedup mechanism.
        """
        timestamp = record.get("published_at") or record.get("valid_from")
        published = self._parse_dt(timestamp)
        if published is None:
            return False
        age_hours = (datetime.now() - published).total_seconds() / 3600
        return age_hours > max_age_hours
