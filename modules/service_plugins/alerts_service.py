#!/usr/bin/env python3
"""
Alerts Service for MeshCore Bot (Phase 2a)

Background push service — polls official Bialystok/Podlaskie alert sources on a
configurable interval and automatically posts new alerts to a MeshCore channel.
Not a user command: nobody has to ask for this, it fires on its own.

Sources (see plans/alert-feeds-research.md and plans/phase2-alert-integration-plan.md):
- IMGW meteorological warnings, filtered to warnings touching Bialystok/powiat bialostocki
- RSO (Regionalny System Ostrzegania) for wojewodztwo podlaskie, which also carries
  republished Alert RCB messages

GIOS air quality and PAA radiation monitoring are Phase 2b — not implemented here.

Architected as a near-direct port of EarthquakeService's poll-loop/dedup/send pattern
(modules/service_plugins/earthquake_service.py), run twice — once per source — so a
slow or failing source never blocks the other.
"""

import asyncio
import contextlib
import json
from collections import deque
from datetime import datetime
from typing import Any, Callable, Coroutine, Optional

import requests

from ..clients import alert_sources
from ..utils import truncate_string
from .base_service import BaseServicePlugin

# Same conservative single-message budget CLAUDE.md documents for channel sends
# (~148 chars after the bot's username prefix); alerts are compact by design.
COMPACT_MESSAGE_MAX_LEN = 140

# Per-source dedup state (bot_metadata, JSON-encoded list of external_ids, most
# recent last) is capped at this many entries so it never grows unbounded — the
# practical "watermark" the phase 2 plan describes, just id-set-shaped rather than
# timestamp-shaped since both sources already hand back a small, already-filtered
# "currently active" list with stable ids (no need for USGS-style time windowing).
SEEN_IDS_MAX = 200

METADATA_KEY_IMGW_SEEN = "alerts_imgw_meteo_seen_ids"
METADATA_KEY_RSO_SEEN = "alerts_rso_seen_ids"


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
        {"key": "rso_enabled", "label": "RSO enabled", "type": "bool", "default": True,
         "help": "Poll RSO (Regionalny System Ostrzegania) for wojewodztwo podlaskie."},
        {"key": "rso_poll_interval", "label": "RSO poll interval", "type": "int",
         "min": 30, "default": 300, "unit": "s", "help": "How often to poll RSO."},
        {"key": "rso_send_details", "label": "RSO send full text", "type": "bool", "default": False,
         "help": "Also send the full alert text as a follow-up message."},
        {"key": "rso_max_alerts_per_hour", "label": "RSO alerts/hour cap", "type": "int",
         "min": 1, "default": 12, "help": "Ceiling on RSO alerts sent per rolling hour."},
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

        self.rso_enabled = self.bot.config.getboolean(section, "rso_enabled", fallback=True)
        self.rso_poll_interval = self.bot.config.getint(section, "rso_poll_interval", fallback=300)
        self.rso_send_details = self.bot.config.getboolean(section, "rso_send_details", fallback=False)
        self.rso_max_alerts_per_hour = self.bot.config.getint(
            section, "rso_max_alerts_per_hour", fallback=12
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

        self.logger.info(
            "Alerts service initialized: channel=%s, imgw=%s (every %ss), rso=%s (every %ss)",
            self.channel,
            self.imgw_enabled,
            self.imgw_poll_interval,
            self.rso_enabled,
            self.rso_poll_interval,
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
    # Poll loop (shared by both sources; each source runs its own instance
    # of this loop as an independent task, so one source's failure/timeout
    # never delays or blocks the other)
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
        )

    def _format_imgw_message(self, record: dict[str, Any]) -> str:
        prefix = self._translate("services.alerts.imgw_meteo.prefix")
        title = record.get("title") or "?"
        severity = record.get("severity") or "?"
        valid_to = self._format_dt_short(record.get("valid_to"))
        text = f"⚠️ {prefix}: {title} (st.{severity})"
        if valid_to:
            until = self._translate("services.alerts.until", time=valid_to)
            text = f"{text} — {until}"
        return truncate_string(text, COMPACT_MESSAGE_MAX_LEN)

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
        )

    def _format_rso_message(self, record: dict[str, Any]) -> str:
        prefix = self._translate("services.alerts.rso.prefix")
        title = record.get("title") or "?"
        description = record.get("description") or ""
        base = f"📢 {prefix}: {title}"
        if description and description != title:
            remaining = COMPACT_MESSAGE_MAX_LEN - len(base) - 3  # " — "
            if remaining > 10:
                base = f"{base} — {truncate_string(description, remaining)}"
        return truncate_string(base, COMPACT_MESSAGE_MAX_LEN)

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
    ) -> None:
        seen = self._seen_ids[source]
        new_records = [
            r for r in records
            if r.get("external_id") and r.get("external_id") not in seen
        ]
        if not new_records:
            return  # nothing new this poll -- send nothing (requirement: no-op on no new alerts)

        # Oldest first, so a burst of several new alerts posts in chronological order.
        new_records.sort(key=lambda r: r.get("published_at") or "")

        sent_count = 0
        suppressed_count = 0
        for record in new_records:
            external_id = record["external_id"]
            # Mark as seen regardless of whether the rate cap allows sending it now --
            # this is a hard per-hour ceiling (log-and-drop), not a deferred queue, so
            # a suppressed alert is not retried once the cap resets next poll.
            self._mark_seen(source, external_id)

            if not self._check_rate_limit(source, max_alerts_per_hour):
                suppressed_count += 1
                continue

            try:
                await self._send_alert(record, formatter, send_details)
                sent_count += 1
                self._sent_timestamps[source].append(datetime.now().timestamp())
            except Exception as e:
                self.logger.error(
                    "Error sending %s alert %s: %s", source, external_id, e
                )

        if suppressed_count:
            self.logger.warning(
                "%s: %d alert(s) suppressed this poll (over %d/hour cap)",
                source, suppressed_count, max_alerts_per_hour,
            )
        if sent_count:
            self.logger.info("%s: sent %d new alert(s)", source, sent_count)

        self._persist_seen_ids(source)

    async def _send_alert(
        self,
        record: dict[str, Any],
        formatter: Callable[[dict[str, Any]], str],
        send_details: bool,
    ) -> None:
        text = formatter(record)
        await self.bot.command_manager.send_channel_message(
            self.channel, text, scope=self.get_mesh_flood_scope()
        )
        if send_details:
            description = record.get("description")
            if description:
                await self.bot.command_manager.send_channel_message(
                    self.channel,
                    truncate_string(description, COMPACT_MESSAGE_MAX_LEN),
                    scope=self.get_mesh_flood_scope(),
                )

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

    @staticmethod
    def _format_dt_short(value: Optional[str]) -> Optional[str]:
        """'2026-08-10 19:00:00' -> '19:00 10.08'; returns None/raw on parse failure."""
        if not value:
            return None
        try:
            dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
            return dt.strftime("%H:%M %d.%m")
        except ValueError:
            return value
