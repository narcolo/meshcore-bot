"""Base class for threshold-crossing alert sources (GIOS): a continuous measurement
with no natural "new item" to dedup, so it alerts on crossing a configured threshold
instead, with hysteresis so it doesn't re-fire every poll while sitting above the line.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable, Optional

from .base import AlertSourceBase


class ThresholdAlertSourceBase(AlertSourceBase):
    schedule_kind = "interval"

    def __init__(self, service: Any) -> None:
        super().__init__(service)
        # station -> {"state": "normal"|"elevated", "last_notified_at": iso str | None}.
        # Keyed by station for consistency/future multi-station support, even though
        # GIOS today only ever has the one configured station.
        self._state: dict[str, dict[str, Any]] = self._load_threshold_state()

    @property
    def _metadata_key_state(self) -> str:
        return f"alerts_{self.name}_threshold_state"

    async def _apply_threshold_state(
        self,
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
        entry = self._state.get(station_key) or {"state": "normal", "last_notified_at": None}
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

        self._state[station_key] = entry
        return text

    def _load_threshold_state(self) -> dict[str, dict[str, Any]]:
        db_manager = getattr(self.service.bot, "db_manager", None)
        if not db_manager:
            return {}
        raw = db_manager.get_metadata(self._metadata_key_state)
        if not raw:
            return {}
        try:
            state = json.loads(raw)
        except (ValueError, TypeError) as e:
            self.logger.warning("Could not parse persisted %s threshold state: %s", self.name, e)
            return {}
        return state if isinstance(state, dict) else {}

    def _persist_threshold_state(self) -> None:
        db_manager = getattr(self.service.bot, "db_manager", None)
        if not db_manager:
            return
        db_manager.set_metadata(self._metadata_key_state, json.dumps(self._state))
