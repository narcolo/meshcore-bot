"""Fetch + normalize functions for the alert sources (IMGW, RSO — Phase 2a; GIOS, PAA —
Phase 2b).

Synchronous (plain ``requests``), by design — callers run these in a thread executor
(see ``AlertsService``), matching the pattern already used by
``modules/service_plugins/earthquake_service.py`` for its USGS polling.

See ``plans/alert-feeds-research.md`` for the source research this is built from, and
``poc_alerts/fetch_alerts_poc.py`` for the original proof-of-concept these functions were
promoted from (IMGW fetch/normalize logic is carried over near-verbatim; RSO, GIOS, PAA
are new). GIOS/PAA return purpose-built measurement dicts, not the discrete-event
``_normalize()`` shape IMGW/RSO use — they're continuous readings, not "new item"
events, and are consumed by AlertsService's separate threshold/hysteresis logic.
"""

import re
import unicodedata
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

GIOS_AQINDEX_URL = "https://api.gios.gov.pl/pjp-api/v1/rest/aqindex/getIndex/{station_id}"

PAA_WFS_URL = "https://monitoring.paa.gov.pl/geoserver/paa/ows"
# A plain non-browser User-Agent gets 403'd by an apparent WAF in front of the
# GeoServer instance (see research doc "6. PAA radiation monitoring") -- no cookies,
# auth, or other headers were needed, just a normal-looking UA string.
PAA_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# Loose bounding box covering wojewodztwo podlaskie and its immediate neighbors --
# wide enough that any plausibly-configured station name is included regardless of
# its exact coordinates, while still avoiding a full-Poland (62-station) payload.
PAA_PODLASKIE_BBOX = "21.5,52.0,24.5,54.5,EPSG:4326"

_PAA_VALUE_RE = re.compile(r"^([\d.]+)\s*(\S+)$")

# Polish ł/Ł don't decompose under NFKD (unlike ą/ć/ę/ń/ó/ś/ź/ż, which do), so a
# plain "strip combining marks" pass alone won't turn "Białystok" into "bialystok".
_POLISH_L_STROKE = str.maketrans("łŁ", "lL")


def normalize_station_name(name: str) -> str:
    """Diacritic- and case-insensitive comparison key, e.g. 'Białystok' ->
    'bialystok', so config.ini can list station names in plain ASCII."""
    decomposed = unicodedata.normalize("NFKD", name.translate(_POLISH_L_STROKE))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).strip().lower()


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


def fetch_gios_aqindex(station_id: int) -> Optional[dict[str, Any]]:
    """Live GIOS overall air quality index for one station.

    Returns None when the station doesn't have enough sensors to compute an
    overall index ("Brak indeksu") -- callers should treat that as "unknown,"
    not "elevated."

    Raises requests.RequestException / ValueError on network/parse failure.
    """
    resp = requests.get(
        GIOS_AQINDEX_URL.format(station_id=station_id),
        headers={"User-Agent": USER_AGENT, "Accept": "application/ld+json"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    # The real payload is JSON-LD: fields live nested under an "AqIndex" key
    # alongside an "@context" block, not at the top level (see research doc
    # "Gotcha found while building the PoC").
    data = resp.json().get("AqIndex") or {}
    category = data.get("Nazwa kategorii indeksu")
    if not category or category == "Brak indeksu":
        return None
    return {
        "station_id": station_id,
        "category": category,
        "computed_at": data.get("Data wykonania obliczeń indeksu"),
    }


def fetch_paa_radiation(station_names: list[str]) -> list[dict[str, Any]]:
    """Live PAA radiation dose-rate readings for the given station names.

    Only stations whose "stacja" name case-insensitively matches one of
    station_names are returned; a configured name absent from the response is
    simply missing from the result (callers should log if that's unexpected).

    Raises requests.RequestException / ValueError on network/parse failure.
    """
    wanted = {normalize_station_name(name) for name in station_names if name.strip()}
    resp = requests.get(
        PAA_WFS_URL,
        params={
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": "paa:kcad_siec_pms_moc_dawki_mapa",
            "outputFormat": "application/json",
            "bbox": PAA_PODLASKIE_BBOX,
        },
        headers={"User-Agent": PAA_USER_AGENT},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    readings = []
    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        name = (props.get("stacja") or "").strip()
        if normalize_station_name(name) not in wanted:
            continue
        match = _PAA_VALUE_RE.match((props.get("tip_value") or "").strip())
        if not match:
            continue
        readings.append({
            "station": name,
            "value": float(match.group(1)),
            "unit": match.group(2),
            "timestamp": props.get("tip_date"),
        })
    return readings
