"""Base class for "new item appeared" alert sources (IMGW, RSO): each check() fetches
a list of discrete records, and any record whose external_id hasn't been seen before
gets sent (subject to a staleness filter and a rolling-hour rate cap) and remembered.
"""

from __future__ import annotations

import json
from collections import deque
from datetime import datetime
from typing import Any, Callable

from .base import AlertSourceBase

# Bounded so a long-running process doesn't grow the persisted seen-ids list forever;
# large enough that no plausible burst of alerts evicts an id before it'd naturally
# stop being re-fetched by the upstream source.
SEEN_IDS_MAX = 200


class EventAlertSourceBase(AlertSourceBase):
    schedule_kind = "interval"

    def __init__(self, service: Any) -> None:
        super().__init__(service)
        self._seen_ids: set[str] = self._load_seen_ids()
        self._seen_order: deque = deque(self._seen_ids, maxlen=SEEN_IDS_MAX)
        self._sent_timestamps: deque = deque()

    @property
    def _metadata_key_seen(self) -> str:
        return f"alerts_{self.name}_seen_ids"

    # ------------------------------------------------------------------
    # New-record processing
    # ------------------------------------------------------------------

    async def _process_new_alerts(
        self,
        records: list[dict[str, Any]],
        formatter: Callable[[dict[str, Any]], str],
        send_details: bool,
        max_alerts_per_hour: int,
        max_age_hours: float,
    ) -> None:
        new_records = [
            r for r in records if r.get("external_id") and str(r["external_id"]) not in self._seen_ids
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
                self._mark_seen(str(record["external_id"]))
                stale_count += 1
            else:
                fresh_records.append(record)
        if stale_count:
            self.logger.info(
                "%s: skipped %d stale alert(s) (published more than %gh ago)",
                self.name, stale_count, max_age_hours,
            )
        new_records = fresh_records

        # Oldest first, so a burst of several new alerts posts in chronological order.
        new_records.sort(key=lambda r: r.get("published_at") or "")

        sent_count = 0
        suppressed_count = 0
        failed_count = 0
        for record in new_records:
            external_id = str(record["external_id"])

            if not self._check_rate_limit(max_alerts_per_hour):
                # Hard per-hour ceiling (log-and-drop), not a deferred queue -- mark
                # seen so a suppressed alert is not retried once the cap resets.
                self._mark_seen(external_id)
                suppressed_count += 1
                continue

            # No manual pacing needed here: _send_alert goes through
            # send_channel_messages_chunked, which (a) skips the reject-on-limit
            # global rate limiter for automated services and (b) still blocks on
            # the bot's shared TX pacer (bot_tx_rate_limiter) on every send, so a
            # burst is naturally spaced without any of it being lost.
            try:
                sent = await self._send_alert(record, formatter, send_details)
            except Exception as e:
                self.logger.error("Error sending %s alert %s: %s", self.name, external_id, e)
                sent = False

            if sent:
                self._mark_seen(external_id)
                sent_count += 1
                self._sent_timestamps.append(datetime.now().timestamp())
            else:
                # Transient failure -- deliberately NOT marked seen, so this alert
                # is retried on the next poll instead of being silently lost.
                failed_count += 1

        if suppressed_count:
            self.logger.warning(
                "%s: %d alert(s) suppressed this poll (over %d/hour cap)",
                self.name, suppressed_count, max_alerts_per_hour,
            )
        if failed_count:
            self.logger.warning(
                "%s: %d alert(s) failed to send this poll, will retry next poll", self.name, failed_count,
            )
        if sent_count:
            self.logger.info("%s: sent %d new alert(s)", self.name, sent_count)

        self._persist_seen_ids()

    async def _send_alert(
        self,
        record: dict[str, Any],
        formatter: Callable[[dict[str, Any]], str],
        send_details: bool,
    ) -> bool:
        text = formatter(record)
        sent = await self._send_simple_message(text)
        if not sent:
            return False
        if send_details:
            description = record.get("description")
            if description and description not in text:
                await self._send_simple_message(description)
        return True

    def _check_rate_limit(self, max_alerts_per_hour: int) -> bool:
        now = datetime.now().timestamp()
        while self._sent_timestamps and now - self._sent_timestamps[0] > 3600:
            self._sent_timestamps.popleft()
        return len(self._sent_timestamps) < max_alerts_per_hour

    def _is_stale(self, record: dict[str, Any], max_age_hours: float) -> bool:
        published = self._parse_dt(record.get("published_at") or record.get("valid_from"))
        if published is None:
            # Fail open: an alert we can't date is more likely a format we don't
            # recognize than one that's actually old -- better to send it than drop it.
            return False
        age_hours = (datetime.now() - published).total_seconds() / 3600
        return age_hours > max_age_hours

    # ------------------------------------------------------------------
    # Seen-ids persistence (bot_metadata, JSON list -- no new DB table)
    # ------------------------------------------------------------------

    def _load_seen_ids(self) -> set[str]:
        db_manager = getattr(self.service.bot, "db_manager", None)
        if not db_manager:
            return set()
        raw = db_manager.get_metadata(self._metadata_key_seen)
        if not raw:
            return set()
        try:
            ids = json.loads(raw)
        except (ValueError, TypeError) as e:
            self.logger.warning("Could not parse persisted %s seen-ids: %s", self.name, e)
            return set()
        return {str(i) for i in ids} if isinstance(ids, list) else set()

    def _mark_seen(self, external_id: str) -> None:
        if external_id in self._seen_ids:
            return
        if len(self._seen_order) == self._seen_order.maxlen:
            evicted = self._seen_order.popleft()
            self._seen_ids.discard(evicted)
        self._seen_order.append(external_id)
        self._seen_ids.add(external_id)

    def _persist_seen_ids(self) -> None:
        db_manager = getattr(self.service.bot, "db_manager", None)
        if not db_manager:
            return
        db_manager.set_metadata(self._metadata_key_seen, json.dumps(list(self._seen_order)))
