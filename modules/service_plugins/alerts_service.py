#!/usr/bin/env python3
"""
Alerts Service for MeshCore Bot (Phase 2a + 2b)

Background push service — polls official Bialystok/Podlaskie alert sources on a
configurable interval and automatically posts new alerts to a MeshCore channel.
Not a user command: nobody has to ask for this, it fires on its own.

This class is a thin orchestrator: config, source discovery, and the two loop
primitives (_poll_loop / _daily_schedule_loop). Each concrete source (IMGW, RSO,
GIOS, PAA, ...) lives as a self-contained module in alert_modules/ and is found
automatically -- see _discover_sources(), which mirrors the same
inspect-based discovery ServicePluginLoader.load_service() uses one level up
(modules/service_plugin_loader.py) to find *this* class in modules/service_plugins/.
Adding/removing a source is therefore just adding/removing a file in
alert_modules/ -- no changes needed here, and no new [Alerts_Service] config keys
beyond whatever the source itself reads.

Three different push shapes live across these sources (see alert_modules/base.py,
event_source.py, threshold_source.py):
- IMGW/RSO are discrete *events* with stable ids -- "is this new?" dedup
  (EventAlertSourceBase), polled on a fixed interval.
- GIOS is a continuous *measurement* with no natural "new item" -- it alerts on
  threshold crossing instead, with hysteresis so it doesn't re-fire every poll
  while sitting above the line (ThresholdAlertSourceBase), also on a fixed interval.
- PAA is a *scheduled digest*: fetches all configured stations and pushes one
  combined message at fixed times of day (default 06:00/18:00), always, regardless
  of readings (plain AlertSourceBase, schedule_kind="daily") -- not an interval,
  and not threshold-gated.

Poll loops run one per source, all independent, so a slow or failing source never
delays or blocks the others.
"""

import asyncio
import contextlib
import importlib
import inspect
import pkgutil
from datetime import datetime, time, timedelta
from typing import Any, Callable, Coroutine, Optional

from . import alert_modules
from .alert_modules.base import AlertSourceBase
from .base_service import BaseServicePlugin


class AlertsService(BaseServicePlugin):
    """Discovers and drives per-source alert modules, pushing to a MeshCore channel."""

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
        {"key": "imgw_meteo_teryt_codes", "label": "IMGW TERYT codes", "type": "str",
         "default": "2061,2002",
         "help": "Comma-separated TERYT codes (Polish administrative-unit identifiers) to "
                 "filter warnings to. Default = city of Bialystok (2061) + powiat bialostocki "
                 "(2002). To find yours, see config.ini.example's comment above this key."},
        {"key": "imgw_meteo_area_label", "label": "IMGW area label", "type": "str",
         "default": "Bialystok / powiat bialostocki",
         "help": "Human-readable label for the configured TERYT codes; available as {area} "
                 "in imgw_meteo_template."},
        {"key": "imgw_meteo_poll_interval", "label": "IMGW poll interval", "type": "int",
         "min": 30, "default": 300, "unit": "s", "help": "How often to poll IMGW."},
        {"key": "imgw_meteo_send_details", "label": "IMGW send full text", "type": "bool", "default": False,
         "help": "Also send the full warning text as a follow-up message."},
        {"key": "imgw_meteo_max_alerts_per_hour", "label": "IMGW alerts/hour cap", "type": "int",
         "min": 1, "default": 12, "help": "Ceiling on IMGW alerts sent per rolling hour."},
        {"key": "imgw_meteo_max_age_hours", "label": "IMGW max alert age", "type": "int",
         "min": 1, "default": 24, "unit": "h",
         "help": "Skip warnings published longer ago than this (stale-dedup-reset guard)."},
        {"key": "imgw_meteo_template", "label": "IMGW message template", "type": "str", "default": "",
         "help": "Preset name (compact/minimal/detailed) or a custom format string. Empty = compact."},
        {"key": "rso_enabled", "label": "RSO enabled", "type": "bool", "default": True,
         "help": "Poll RSO (Regionalny System Ostrzegania) for rso_wojewodztwo."},
        {"key": "rso_wojewodztwo", "label": "RSO wojewodztwo", "type": "str", "default": "podlaskie",
         "help": "Lowercase wojewodztwo slug used in the RSO URL. See config.ini.example's "
                 "comment above this key for the full list of valid slugs."},
        {"key": "rso_poll_interval", "label": "RSO poll interval", "type": "int",
         "min": 30, "default": 300, "unit": "s", "help": "How often to poll RSO."},
        {"key": "rso_send_details", "label": "RSO send full text", "type": "bool", "default": False,
         "help": "Also send the full alert text as a follow-up message."},
        {"key": "rso_max_alerts_per_hour", "label": "RSO alerts/hour cap", "type": "int",
         "min": 1, "default": 12, "help": "Ceiling on RSO alerts sent per rolling hour."},
        {"key": "rso_max_age_hours", "label": "RSO max alert age", "type": "int",
         "min": 1, "default": 24, "unit": "h",
         "help": "Skip alerts published longer ago than this (stale-dedup-reset guard)."},
        {"key": "rso_template", "label": "RSO message template", "type": "str", "default": "",
         "help": "Preset name (compact/minimal/title_only) or a custom format string. Empty = compact."},
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
        {"key": "gios_template", "label": "GIOS message template", "type": "str", "default": "",
         "help": "Preset name (compact/minimal) or a custom format string. Empty = compact."},
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
        {"key": "paa_bbox", "label": "PAA bounding box", "type": "str",
         "default": "21.5,52.0,24.5,54.5,EPSG:4326",
         "help": "WFS bounding box (min_lon,min_lat,max_lon,max_lat,EPSG:4326) limiting which "
                 "PAA stations are queried -- must cover your paa_stations' real coordinates "
                 "or they'll never be returned. See config.ini.example's comment above this key."},
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
        {"key": "paa_template", "label": "PAA message template", "type": "str", "default": "",
         "help": "Preset name (compact) or a custom format string. Empty = compact."},
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)

        section = self.config_section
        self.channel = self.bot.config.get(section, "channel", fallback="general")

        self._tasks: list[asyncio.Task] = []
        self._sources: list[AlertSourceBase] = [cls(self) for cls in self._discover_source_classes()]

        self.logger.info(
            "Alerts service initialized: channel=%s, sources=%s",
            self.channel, [s.name for s in self._sources],
        )

    # ------------------------------------------------------------------
    # Source discovery
    # ------------------------------------------------------------------

    def _discover_source_classes(self) -> list[type[AlertSourceBase]]:
        """Import every module in alert_modules/ and collect the one AlertSourceBase
        subclass each defines -- same inspect.getmembers + issubclass + module-identity
        pattern ServicePluginLoader.load_service() uses to find service plugin classes
        (modules/service_plugin_loader.py:90-144), one package level down."""
        classes: list[type[AlertSourceBase]] = []
        for module_info in pkgutil.iter_modules(alert_modules.__path__):
            module_name = module_info.name
            if module_name in ("base", "event_source", "threshold_source"):
                continue
            module_path = f"{alert_modules.__name__}.{module_name}"
            module = importlib.import_module(module_path)
            for _name, obj in inspect.getmembers(module, inspect.isclass):
                if (
                    issubclass(obj, AlertSourceBase)
                    and obj not in (AlertSourceBase,)
                    and obj.__module__ == module_path
                ):
                    classes.append(obj)
                    break
        return classes

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if not self.enabled:
            self.logger.info("Alerts service is disabled, not starting")
            return
        self._running = True
        self.logger.info("Starting alerts service")

        for source in self._sources:
            if not source.is_enabled():
                continue
            if source.schedule_kind == "daily":
                coro = self._daily_schedule_loop(
                    source.name, source.check, source.schedule_times(), f"alerts_{source.name}_last_slot"
                )
            else:
                coro = self._poll_loop(source.name, source.check, source.poll_interval())
            self._tasks.append(asyncio.create_task(coro))

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
    # Poll loop (interval-based): each enabled source runs its own instance as
    # an independent task, so one source's failure/timeout never delays or
    # blocks the others.
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
