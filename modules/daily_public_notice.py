#!/usr/bin/env python3
"""
Daily Public Notice: periodic onboarding notice for new MeshCore users.

Pure config-parsing / message-rendering logic for the ``[Daily_Public_Notice]``
scheduled feature. Kept separate from ``scheduler.py`` (which owns the
APScheduler job and the actual send) so it can be unit tested without
APScheduler/threading — mirrors the split ``scheduled_message_cron.py`` uses
for ``[Scheduled_Messages]``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .flood_scope import normalize_scope_name

SECTION = "Daily_Public_Notice"
MESSAGE_KEY_PREFIX = "message."
SCOPES_PLACEHOLDER = "{scopes}"


@dataclass(frozen=True)
class DailyPublicNoticeConfig:
    """Parsed, validated ``[Daily_Public_Notice]`` configuration."""

    hour: int
    minute: int
    channel: str
    flood_scope: str
    recommended_scopes: tuple[str, ...]
    max_message_bytes: int
    message_delay_seconds: float
    message_templates: tuple[str, ...]  # in ascending message.N order


def _parse_time(raw: str) -> Optional[tuple[int, int]]:
    """Parse an ``HH:MM`` (24h) string, or None if malformed/out of range."""
    raw = (raw or "").strip()
    if ":" not in raw:
        return None
    hh, _, mm = raw.partition(":")
    if not hh.isdigit() or not mm.isdigit():
        return None
    hour, minute = int(hh), int(mm)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _numbered_messages(config: Any, section: str) -> list[str]:
    """Return ``message.N`` values from ``section``, sorted by ascending N."""
    entries: list[tuple[int, str]] = []
    for key, value in config.items(section):
        if not key.lower().startswith(MESSAGE_KEY_PREFIX):
            continue
        suffix = key[len(MESSAGE_KEY_PREFIX):]
        if not suffix.isdigit():
            continue
        entries.append((int(suffix), value))
    entries.sort(key=lambda e: e[0])
    return [v for _, v in entries]


def load_config(bot_config: Any, logger: Any = None) -> Optional[DailyPublicNoticeConfig]:
    """Parse ``[Daily_Public_Notice]`` into a config object.

    Returns None when the section is absent, ``enabled`` is false, or the
    configuration is invalid in a way that makes scheduling unsafe (invalid
    time, empty channel, non-positive byte limit, negative delay, or no
    ``message.N`` entries) — a clear error is logged for the invalid cases so
    the caller can simply skip setting up the job for this run/reload.
    """
    if not bot_config.has_section(SECTION):
        return None
    if not bot_config.getboolean(SECTION, "enabled", fallback=False):
        return None

    time_raw = bot_config.get(SECTION, "time", fallback="19:00")
    parsed_time = _parse_time(time_raw)
    if parsed_time is None:
        if logger:
            logger.error(
                "Daily_Public_Notice: invalid time '%s' (expected HH:MM, 24h); disabling",
                time_raw,
            )
        return None
    hour, minute = parsed_time

    channel = (bot_config.get(SECTION, "channel", fallback="Public") or "").strip()
    if not channel:
        if logger:
            logger.error("Daily_Public_Notice: channel is empty; disabling")
        return None

    # Canonical '#'-prefixed form (a global marker stays as-is); "west" and
    # "#west" in config are the same region.
    flood_scope = normalize_scope_name(bot_config.get(SECTION, "flood_scope", fallback="pl-podlasie"))

    recommended_raw = bot_config.get(SECTION, "recommended_scopes", fallback="")
    recommended_scopes = tuple(s.strip() for s in recommended_raw.split(",") if s.strip())

    try:
        max_message_bytes = bot_config.getint(SECTION, "max_message_bytes", fallback=120)
    except ValueError:
        if logger:
            logger.error("Daily_Public_Notice: max_message_bytes is not an integer; disabling")
        return None
    if max_message_bytes <= 0:
        if logger:
            logger.error(
                "Daily_Public_Notice: max_message_bytes must be > 0 (got %s); disabling",
                max_message_bytes,
            )
        return None

    try:
        message_delay_seconds = bot_config.getfloat(SECTION, "message_delay_seconds", fallback=5.0)
    except ValueError:
        if logger:
            logger.error("Daily_Public_Notice: message_delay_seconds is not a number; disabling")
        return None
    if message_delay_seconds < 0:
        if logger:
            logger.error(
                "Daily_Public_Notice: message_delay_seconds must be >= 0 (got %s); disabling",
                message_delay_seconds,
            )
        return None

    templates = _numbered_messages(bot_config, SECTION)
    if not templates:
        if logger:
            logger.error("Daily_Public_Notice: no message.N entries configured; disabling")
        return None

    return DailyPublicNoticeConfig(
        hour=hour,
        minute=minute,
        channel=channel,
        flood_scope=flood_scope,
        recommended_scopes=recommended_scopes,
        max_message_bytes=max_message_bytes,
        message_delay_seconds=message_delay_seconds,
        message_templates=tuple(templates),
    )


def render_messages(cfg: DailyPublicNoticeConfig, logger: Any = None) -> Optional[list[str]]:
    """Render every ``message.N`` template, reducing ``{scopes}`` to fit.

    For a template containing ``{scopes}``, the recommended-scopes list is
    rendered in configured order; if the full list does not fit within
    ``max_message_bytes``, trailing scopes are dropped one at a time (never
    partially truncated) until it fits, or until none remain.

    Returns the ordered list of final message strings to send, or None if
    any message still exceeds ``max_message_bytes`` after full reduction —
    the caller must then skip the entire notice batch for this run rather
    than send a partial/truncated one. A clear error naming the failing
    ``message.N``, its rendered byte length, and the configured limit is
    logged in that case.
    """
    rendered_messages: list[str] = []
    for idx, template in enumerate(cfg.message_templates, start=1):
        if SCOPES_PLACEHOLDER in template:
            remaining = list(cfg.recommended_scopes)
            rendered = template.replace(SCOPES_PLACEHOLDER, ", ".join(remaining))
            while len(rendered.encode("utf-8")) > cfg.max_message_bytes and remaining:
                remaining.pop()
                rendered = template.replace(SCOPES_PLACEHOLDER, ", ".join(remaining))
        else:
            rendered = template

        byte_length = len(rendered.encode("utf-8"))
        if byte_length > cfg.max_message_bytes:
            if logger:
                logger.error(
                    "Daily_Public_Notice: message.%d does not fit max_message_bytes "
                    "(rendered %d bytes > limit %d); skipping the entire notice batch",
                    idx, byte_length, cfg.max_message_bytes,
                )
            return None
        rendered_messages.append(rendered)
    return rendered_messages
