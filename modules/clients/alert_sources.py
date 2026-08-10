"""Fetch + normalize functions for the Phase 2a alert sources (IMGW, RSO).

Synchronous (plain ``requests``), by design — callers run these in a thread executor
(see ``AlertsService``), matching the pattern already used by
``modules/service_plugins/earthquake_service.py`` for its USGS polling.

See ``plans/alert-feeds-research.md`` for the source research this is built from, and
``poc_alerts/fetch_alerts_poc.py`` for the original proof-of-concept these functions were
promoted from (IMGW fetch/normalize logic is carried over near-verbatim; RSO is new).
"""

from typing import Any, Optional

import requests

TIMEOUT = 20
USER_AGENT = "meshcore-bot-alerts/1.0 (+https://github.com/agessaman/meshcore-bot)"

# TERYT codes confirmed live during research (2026-08-10): 2061 = miasto Bialystok,
# 2002 = powiat bialostocki. IMGW meteo warnings carry a teryt[] array per warning.
BIALYSTOK_TERYT = {"2061", "2002"}

IMGW_METEO_WARNINGS_URL = "https://danepubliczne.imgw.pl/api/data/warningsmeteo"

# komunikaty.tvp.pl is RSO's actual backend (see research doc "1a. RSO"); the
# wojewodztwo slug in the path scopes results to Podlaskie server-side, so no
# additional area filtering is needed on the response.
RSO_PODLASKIE_URL = "http://komunikaty.tvp.pl/komunikaty/podlaskie/wszystkie?_format=json"


def _normalize(
    source: str,
    external_id: Optional[str],
    type_: str,
    severity: Optional[str],
    area: Optional[str],
    title: Optional[str],
    description: Optional[str],
    published_at: Optional[str],
    valid_from: Optional[str],
    valid_to: Optional[str],
    url: Optional[str],
) -> dict[str, Any]:
    return {
        "source": source,
        "external_id": external_id,
        "type": type_,
        "severity": severity,
        "area": area,
        "title": title,
        "description": description,
        "published_at": published_at,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "url": url,
    }


def fetch_imgw_meteo_warnings() -> list[dict[str, Any]]:
    """Live IMGW meteorological warnings, filtered to ones whose teryt[] touches Bialystok.

    Raises requests.RequestException / ValueError on network/parse failure — callers
    are expected to catch and log, same as EarthquakeService._check_earthquakes does
    for its USGS call.
    """
    resp = requests.get(
        IMGW_METEO_WARNINGS_URL,
        headers={"User-Agent": USER_AGENT},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    warnings = resp.json()

    records = []
    for w in warnings:
        teryt = set(w.get("teryt") or [])
        if not teryt & BIALYSTOK_TERYT:
            continue
        records.append(
            _normalize(
                source="imgw_meteo",
                external_id=w.get("id"),
                type_="weather",
                severity=w.get("stopien"),
                area="Bialystok / powiat bialostocki",
                title=w.get("nazwa_zdarzenia"),
                description=w.get("tresc"),
                published_at=w.get("opublikowano"),
                valid_from=w.get("obowiazuje_od"),
                valid_to=w.get("obowiazuje_do"),
                url=None,  # IMGW's warnings API does not provide a per-warning article URL
            )
        )
    return records


def fetch_rso_podlaskie() -> list[dict[str, Any]]:
    """Live RSO (Regionalny System Ostrzegania) alerts for wojewodztwo podlaskie.

    Includes Alert RCB messages, which RSO republishes tagged with a title like
    "ALERT RCB - ..." (see research doc "1b. Alert RCB" — there is no standalone
    live RCB feed, this is the practical integration point for it).

    Raises requests.RequestException / ValueError on network/parse failure — callers
    are expected to catch and log.
    """
    resp = requests.get(
        RSO_PODLASKIE_URL,
        headers={"User-Agent": USER_AGENT},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    records = []
    for item in data.get("newses", []):
        item_id = item.get("id")
        provinces = item.get("provinces") or {}
        area = ", ".join(p.get("name", "") for p in provinces.values() if p.get("name")) or "Podlaskie"
        records.append(
            _normalize(
                source="rso",
                external_id=str(item_id) if item_id is not None else None,
                type_="alert",
                severity=item.get("rso_alarm"),
                area=area,
                title=item.get("title"),
                description=item.get("content"),
                published_at=item.get("created_at"),
                valid_from=item.get("valid_from"),
                valid_to=item.get("valid_to"),
                url=None,  # no per-alert article URL confirmed during research
            )
        )
    return records
