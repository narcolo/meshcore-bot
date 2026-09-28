# Alerts Service

Background push service that automatically polls official Polish public-safety
alert feeds and posts new alerts to a MeshCore channel — no user command needed,
it fires on its own. Ships with four sources scoped to Bialystok/Podlaskie by
default, but every source's geographic scope is configurable without touching
code.

| Source | What it is | Push shape |
|---|---|---|
| **IMGW** | Meteorological warnings (storms, heat, frost, ...) | Event dedup |
| **RSO** | Regionalny System Ostrzegania — regional alerts, incl. republished Alert RCB | Event dedup |
| **GIOS** | Air quality index for one station | Threshold + hysteresis |
| **PAA** | Radiation dose-rate for a list of stations | Scheduled digest |

---

## Quick Start

1. **Configure Bot** — edit `config.ini`:

```ini
[Alerts_Service]
enabled = true
channel = #alerts
flood_scope = pl-podlasie

imgw_meteo_enabled = true
rso_enabled = true
gios_enabled = false
paa_enabled = false
```

2. **Restart Bot** — the service discovers and starts a poll loop per enabled
   source; each runs independently, so a slow/failing source never blocks the
   others.

See `config.ini.example`'s `[Alerts_Service]` section for the complete,
commented list of every key below.

---

## Architecture

`AlertsService` (`modules/service_plugins/alerts_service.py`) is a thin
orchestrator: config, source discovery, and two loop primitives (`_poll_loop`
for interval sources, `_daily_schedule_loop` for scheduled ones). It does not
know about IMGW, RSO, GIOS, or PAA by name — it discovers concrete source
classes under `modules/service_plugins/alert_modules/` at construction time,
using the same `inspect`-based pattern `ServicePluginLoader.load_service()`
uses one level up to find service plugins themselves. Adding or removing a
source is therefore just adding or removing a file in that package; the
orchestrator needs no changes.

```
modules/service_plugins/
    alerts_service.py          # orchestrator: config, discovery, start/stop, loop runners
    alert_modules/
        base.py                # AlertSourceBase + shared helpers + template rendering
        event_source.py        # EventAlertSourceBase -- dedup/staleness/rate-cap (IMGW, RSO)
        threshold_source.py    # ThresholdAlertSourceBase -- hysteresis (GIOS)
        imgw_meteo.py           # ImgwMeteoSource(EventAlertSourceBase)
        rso.py                    # RsoSource(EventAlertSourceBase)
        gios.py                     # GiosSource(ThresholdAlertSourceBase)
        paa.py                        # PaaSource(AlertSourceBase, schedule_kind="daily")
```

`modules/clients/alert_sources.py` is a separate, lower layer: plain
synchronous fetch/normalize functions (`fetch_imgw_meteo_warnings`,
`fetch_rso_alerts`, `fetch_gios_aqindex`, `fetch_paa_radiation`), called from
a thread executor so a slow HTTP request never blocks the event loop. This is
the only layer that talks to the actual government APIs.

### Three push shapes

- **Event dedup** (IMGW, RSO): each poll fetches a list of discrete records
  with stable ids. Anything not already in the seen-ids set is a candidate to
  send, subject to a staleness filter (`*_max_age_hours`) and a rolling-hour
  rate cap (`*_max_alerts_per_hour`). Seen ids persist to `bot_metadata`
  (`alerts_<name>_seen_ids`, JSON list, capped at 200 entries) so a restart
  never re-sends something already posted.
- **Threshold + hysteresis** (GIOS): a continuous measurement has no natural
  "new item" to dedup, so it alerts on crossing a configured threshold
  instead. Crossing up sends an alert; staying elevated sends at most one
  "still elevated" reminder per `gios_renotify_hours`; crossing back down
  sends a recovery message once. State persists to `bot_metadata`
  (`alerts_gios_threshold_state`, JSON dict keyed by station) — but note the
  row is only written on an actual *transition*, so "no row yet" just means
  the reading has never crossed the threshold, not an error.
- **Scheduled digest** (PAA): fires unconditionally at fixed wall-clock times
  (`paa_schedule_times`, default `06:00,18:00`), one combined message for
  every configured station regardless of any individual reading. Uses
  `_daily_schedule_loop`, not a plain interval — a plain "sleep N seconds"
  loop fires immediately on every restart and drifts from fixed clock times.
  Instead, this loop computes the most-recently-passed daily slot, fires at
  most once per slot (persisted to `bot_metadata` as
  `alerts_paa_last_slot`), and sleeps exactly until the next boundary.
  **Catch-up behavior is intentional**: if the bot starts after a slot has
  already passed today and that slot hasn't fired yet, it fires immediately
  rather than waiting until tomorrow.

---

## Configuration

All keys live under `[Alerts_Service]`.

### Global

| Key | Description | Default |
|---|---|---|
| `enabled` | Turn the whole service on/off | `false` |
| `channel` | Channel to post alerts to | `general` |
| `flood_scope` | Optional regional TC_FLOOD scope for this service's sends (e.g. `pl-podlasie`). Empty = fall back to `[Channels] outgoing_flood_scope_override`. | *(empty)* |

### IMGW meteorological warnings

| Key | Description | Default |
|---|---|---|
| `imgw_meteo_enabled` | Poll IMGW | `true` |
| `imgw_meteo_poll_interval` | Seconds between polls | `300` |
| `imgw_meteo_send_details` | Also send the full warning text as a follow-up message | `false` |
| `imgw_meteo_max_alerts_per_hour` | Ceiling on alerts sent per rolling hour | `12` |
| `imgw_meteo_max_age_hours` | Skip warnings published longer ago than this | `24` |
| `imgw_meteo_teryt_codes` | Comma-separated TERYT codes to filter to | `2061,2002` (Bialystok + powiat bialostocki) |
| `imgw_meteo_area_label` | Human-readable label, available as `{area}` in the template | `Bialystok / powiat bialostocki` |
| `imgw_meteo_template` | Preset name or custom format string | *(empty = `compact`)* |

### RSO (Regionalny System Ostrzegania)

| Key | Description | Default |
|---|---|---|
| `rso_enabled` | Poll RSO | `true` |
| `rso_poll_interval` | Seconds between polls | `300` |
| `rso_send_details` | Also send the full alert text as a follow-up message | `false` |
| `rso_max_alerts_per_hour` | Ceiling on alerts sent per rolling hour | `12` |
| `rso_max_age_hours` | Skip alerts published longer ago than this | `24` |
| `rso_wojewodztwo` | Wojewodztwo slug (see [Region portability](#region-portability)) | `podlaskie` |
| `rso_template` | Preset name or custom format string | *(empty = `compact`)* |

### GIOS air quality

| Key | Description | Default |
|---|---|---|
| `gios_enabled` | Poll GIOS | `false` |
| `gios_poll_interval` | Seconds between polls | `900` |
| `gios_station_id` | GIOS station to monitor | `11174` (ul. 42 Pułku Piechoty, Bialystok) |
| `gios_alert_category` | Alert when the AQI category reaches or exceeds this (Polish 6-level scale, worst→best: `Bardzo zły`, `Zły`, `Dostateczny`, `Umiarkowany`, `Dobry`, `Bardzo dobry`) | `Zły` |
| `gios_renotify_hours` | Minimum hours between repeat "still elevated" reminders | `6` |
| `gios_template` | Preset name or custom format string | *(empty = `compact`)* |

### PAA radiation monitoring

| Key | Description | Default |
|---|---|---|
| `paa_enabled` | Push the digest | `false` |
| `paa_schedule_times` | Comma-separated daily `HH:MM` times (local time) | `06:00,18:00` |
| `paa_stations` | Comma-separated station names, in display order (plain ASCII, diacritic/case-insensitive matching) | `Bialystok,Suwalki,Siemiatycze` |
| `paa_bbox` | WFS bounding box limiting which stations are even queried (see [Region portability](#region-portability)) | `21.5,52.0,24.5,54.5,EPSG:4326` |
| `paa_alert_dose_rate_usvh` | Cosmetic warning-marker threshold — **not** an official PAA safety standard | `0.3` |
| `paa_max_reading_age_hours` | Show a station as "no data" rather than an actually-stale reading | `6` |
| `paa_fallback_max_km` | A station with no fresh reading is replaced by the nearest station with a fresh one within this many km, labelled with its real name and distance (`Bialystok: 0.07 (Waliły Stacja, 39 km)`); `0` disables. Candidates must lie inside `paa_bbox` | `50` |
| `paa_template` | Preset name or custom format string | *(empty = `compact`)* |

---

## Message templates

Every source's default output ("compact") reproduces the original hand-coded
message format exactly. Each also supports named presets and fully custom
format strings via `<name>_template`:

1. A known preset name (e.g. `minimal`) → uses that preset's format string.
2. Any other non-empty value → used directly as a custom `str.format()`
   template.
3. Empty/unset → the source's default preset (`compact`).

| Source | Presets | Placeholders |
|---|---|---|
| IMGW | `compact` (default), `minimal`, `detailed` | `prefix`, `title`, `severity`, `date`, `until`, `description`, `area`, `date_part`, `until_part` |
| RSO | `compact` (default), `minimal`, `title_only` | `prefix`, `title`, `date`, `description`, `date_part`, `description_part` |
| GIOS | `compact` (default), `minimal` | `icon`, `prefix`, `category`, `date`, `date_part` |
| PAA | `compact` (default) | `prefix`, `when`, `stations` (pre-joined list), `unit` |

`*_part` placeholders (`date_part`, `until_part`, `description_part`) are
pre-assembled by the source with their own separators/brackets already
included, and collapse to an empty string when the underlying value is
missing — so a template referencing `{title}{date_part}` never ends up with a
dangling `" []"` when a record has no date. This is why presets stay plain
`str.format()` strings with no template-language conditionals needed.

A custom template that references an unknown placeholder is caught, logged
once, and falls back to the `compact` preset — a typo can't take a source
down.

---

## Region portability

Nothing about the source/orchestrator code is Bialystok/Podlaskie-specific —
every geographic scope is a config value with a documented default:

| Source | Config key | How to find a value |
|---|---|---|
| IMGW | `imgw_meteo_teryt_codes` | [GUS TERYT registry](https://eteryt.stat.gov.pl/eteryt/rejestr_teryt/udostepnianie_danych/baza_teryt/rejestrterc/rejestrterc.aspx), or inspect a live warning's `teryt[]` array for your area. Usually need both the city code and the surrounding powiat code. |
| RSO | `rso_wojewodztwo` | One of the 16 lowercase wojewodztwo slugs: `dolnoslaskie`, `kujawsko-pomorskie`, `lubelskie`, `lubuskie`, `lodzkie`, `malopolskie`, `mazowieckie`, `opolskie`, `podkarpackie`, `podlaskie`, `pomorskie`, `slaskie`, `swietokrzyskie`, `warminsko-mazurskie`, `wielkopolskie`, `zachodniopomorskie`. |
| GIOS | `gios_station_id` | `GET https://api.gios.gov.pl/pjp-api/v1/rest/station/findAll?page=0&size=500` lists every station; confirm with a live `GET .../aqindex/getIndex/<id>` (header `Accept: application/ld+json`) that it returns a computed index, not `"Brak indeksu"`. |
| PAA | `paa_bbox`, `paa_stations` | Get your area's rough lat/lon range (e.g. via bboxfinder.com) for the bbox; a station outside it is never returned regardless of `paa_stations`. Widen generously — a bigger box costs nothing since station-name matching still happens client-side. |

All defaults above preserve the original Bialystok/Podlaskie behavior exactly
— none of this is a breaking change for an existing deployment.

---

## Adding a new source

Drop a new module in `modules/service_plugins/alert_modules/` with a class
implementing `AlertSourceBase` — usually via `EventAlertSourceBase` (dedup) or
`ThresholdAlertSourceBase` (hysteresis) rather than the bare base. It's picked
up automatically; no changes to `alerts_service.py`, and no new
`[Alerts_Service]` config keys beyond whatever the source itself reads.

Minimal shape:

```python
class MySource(EventAlertSourceBase):  # or ThresholdAlertSourceBase, or AlertSourceBase directly
    name = "my_source"                  # config-key prefix: my_source_enabled, my_source_template, ...

    def is_enabled(self) -> bool:
        return self.bot.config.getboolean(self.service.config_section, "my_source_enabled", fallback=False)

    def poll_interval(self) -> float:   # only for schedule_kind == "interval" (the default)
        return self.bot.config.getint(self.service.config_section, "my_source_poll_interval", fallback=300)

    async def check(self) -> None:      # fetch + process + send
        ...
```

---

## Notes

- No new database table: all persisted state (seen-ids, threshold state,
  last-fired-slot) lives in the existing `bot_metadata` key/value table.
- IMGW/RSO/GIOS each run as an independent `asyncio` task via `_poll_loop`;
  PAA runs via `_daily_schedule_loop`. One source's failure or slow response
  never delays or blocks the others.
- A failed send (e.g. transient bot-level rate-limit contention) is
  deliberately **not** marked seen, so it's retried on the next poll instead
  of being silently lost.
