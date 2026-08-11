"""Unit tests for modules/clients/alert_sources.py (IMGW/RSO - Phase 2a; GIOS/PAA -
Phase 2b fetch/normalize).

Fixture payloads are trimmed real examples captured during the source research in
plans/alert-feeds-research.md -- no network calls are made (requests.get is mocked).
"""

from unittest.mock import Mock, patch

import pytest
import requests

from modules.clients import alert_sources

IMGW_WARNING_TOUCHING_BIALYSTOK = {
    "id": "Wr20260810041907317",
    "nazwa_zdarzenia": "Burze",
    "stopien": "1",
    "prawdopodobienstwo": "70",
    "obowiazuje_od": "2026-08-10 19:00:00",
    "obowiazuje_do": "2026-08-11 03:00:00",
    "opublikowano": "2026-08-10 06:19:00",
    "tresc": "Prognozowane sa lokalne burze, ktorym beda towarzyszyc opady deszczu do "
             "okolo 20 mm oraz porywy wiatru do 80 km/h. Mozliwy grad.",
    "komentarz": "Brak.",
    "biuro": "Centralne Biuro Prognoz Meteorologicznych w Warszawie",
    "teryt": ["2002", "2061", "2003", "2008"],
}

IMGW_WARNING_ELSEWHERE = {
    "id": "Wr20260810041907999",
    "nazwa_zdarzenia": "Upal",
    "stopien": "2",
    "obowiazuje_od": "2026-08-10 12:00:00",
    "obowiazuje_do": "2026-08-11 20:00:00",
    "opublikowano": "2026-08-10 05:00:00",
    "tresc": "Upal w innym rejonie kraju.",
    "teryt": ["1465", "1401"],  # nowhere near Bialystok
}

RSO_RESPONSE = {
    "pagination": {"totalitems": 2, "itemsperpage": 20},
    "newses": [
        {
            "id": 23233017,
            "title": "ALERT RCB - BURZE/2",
            "shortcut": "Pogoda: dzis i w nocy burze z silnym wiatrem.",
            "content": "Pogoda: dzis i w nocy (10/11.08) burze z silnym wiatrem i "
                       "intensywnymi opadami deszczu. Mozliwe przerwy w dostawie pradu.",
            "rso_alarm": "2",
            "rso_icon": None,
            "valid_from": "2026-08-10 13:58:00",
            "valid_to": "2026-08-11 04:00:00",
            "repetition": "5",
            "created_at": "2026-08-10 14:00:52",
            "updated_at": "2026-08-10 14:01:15",
            "provinces": {
                "10": {"id": "10", "name": "Podlaskie", "city": "", "slug_name": "podlaskie"}
            },
        },
        {
            "id": 23232769,
            "title": "Gwaltowne wzrosty stanow wody/1",
            "shortcut": "Obszar: Biebrza, Elk, Lega, Narew.",
            "content": "W obszarach wystepowania prognozowanych opadow burzowych...",
            "rso_alarm": "1",
            "valid_from": "2026-08-10 13:20:00",
            "valid_to": "2026-08-11 02:00:00",
            "created_at": "2026-08-10 12:36:07",
            "provinces": {
                "10": {"id": "10", "name": "Podlaskie", "city": "", "slug_name": "podlaskie"}
            },
        },
    ],
}


def _mock_response(payload):
    resp = Mock()
    resp.status_code = 200
    resp.json.return_value = payload
    resp.raise_for_status = Mock()
    return resp


def _mock_404_response(payload):
    resp = Mock()
    resp.status_code = 404
    resp.json.return_value = payload
    resp.raise_for_status = Mock(side_effect=requests.exceptions.HTTPError("404 Client Error"))
    return resp


@pytest.mark.unit
class TestFetchImgwMeteoWarnings:
    def test_filters_to_teryt_matching_bialystok(self):
        with patch(
            "modules.clients.alert_sources.requests.get",
            return_value=_mock_response(
                [IMGW_WARNING_TOUCHING_BIALYSTOK, IMGW_WARNING_ELSEWHERE]
            ),
        ):
            records = alert_sources.fetch_imgw_meteo_warnings()

        assert len(records) == 1
        record = records[0]
        assert record["source"] == "imgw_meteo"
        assert record["external_id"] == "Wr20260810041907317"
        assert record["type"] == "weather"
        assert record["severity"] == "1"
        assert record["title"] == "Burze"
        assert record["description"].startswith("Prognozowane")
        assert record["published_at"] == "2026-08-10 06:19:00"
        assert record["valid_from"] == "2026-08-10 19:00:00"
        assert record["valid_to"] == "2026-08-11 03:00:00"
        assert record["url"] is None

    def test_warning_touching_only_powiat_code_matches(self):
        warning = dict(IMGW_WARNING_ELSEWHERE, teryt=["2002"])
        with patch(
            "modules.clients.alert_sources.requests.get",
            return_value=_mock_response([warning]),
        ):
            records = alert_sources.fetch_imgw_meteo_warnings()
        assert len(records) == 1

    def test_warning_with_no_teryt_field_is_skipped(self):
        warning = dict(IMGW_WARNING_ELSEWHERE)
        warning.pop("teryt", None)
        with patch(
            "modules.clients.alert_sources.requests.get",
            return_value=_mock_response([warning]),
        ):
            records = alert_sources.fetch_imgw_meteo_warnings()
        assert records == []

    def test_empty_response(self):
        with patch(
            "modules.clients.alert_sources.requests.get",
            return_value=_mock_response([]),
        ):
            records = alert_sources.fetch_imgw_meteo_warnings()
        assert records == []

    def test_404_with_no_products_found_means_zero_warnings_not_a_failure(self):
        """IMGW returns HTTP 404 (not 200 + []) with {"status": false, "message":
        "No products were found"} when there are currently zero active warnings
        nationwide -- confirmed live 2026-08-11. Must be treated as an empty
        result, not raise (the earlier bug: this was surfacing as a spurious
        "IMGW warnings request failed" every poll while no warnings existed)."""
        with patch(
            "modules.clients.alert_sources.requests.get",
            return_value=_mock_404_response({"status": False, "message": "No products were found"}),
        ):
            records = alert_sources.fetch_imgw_meteo_warnings()
        assert records == []

    def test_404_with_a_different_body_still_raises(self):
        """A 404 for some other reason (endpoint moved, bad request, ...) must
        not be silently swallowed -- only the specific known empty-result body
        is treated as non-fatal."""
        with patch(
            "modules.clients.alert_sources.requests.get",
            return_value=_mock_404_response({"error": "not found"}),
        ):
            with pytest.raises(requests.exceptions.HTTPError):
                alert_sources.fetch_imgw_meteo_warnings()

    def test_custom_teryt_codes_filter_to_a_different_region(self):
        """teryt_codes is the actual portability knob -- a deployment for a
        different city passes its own set instead of the Bialystok default."""
        warning = dict(IMGW_WARNING_ELSEWHERE, teryt=["1465"])  # matches neither default code
        with patch(
            "modules.clients.alert_sources.requests.get",
            return_value=_mock_response([IMGW_WARNING_TOUCHING_BIALYSTOK, warning]),
        ):
            records = alert_sources.fetch_imgw_meteo_warnings(teryt_codes={"1465"})
        assert len(records) == 1
        assert records[0]["external_id"] == warning["id"]

    def test_custom_area_label_is_stored_on_every_record(self):
        with patch(
            "modules.clients.alert_sources.requests.get",
            return_value=_mock_response([IMGW_WARNING_TOUCHING_BIALYSTOK]),
        ):
            records = alert_sources.fetch_imgw_meteo_warnings(area_label="Krakow / powiat krakowski")
        assert records[0]["area"] == "Krakow / powiat krakowski"

    def test_default_teryt_codes_and_area_label_match_bialystok(self):
        """Defaults preserve pre-region-config behavior exactly."""
        with patch(
            "modules.clients.alert_sources.requests.get",
            return_value=_mock_response([IMGW_WARNING_TOUCHING_BIALYSTOK]),
        ):
            records = alert_sources.fetch_imgw_meteo_warnings()
        assert records[0]["area"] == "Bialystok / powiat bialostocki"


@pytest.mark.unit
class TestFetchRsoAlerts:
    def test_normalizes_all_fields(self):
        with patch(
            "modules.clients.alert_sources.requests.get",
            return_value=_mock_response(RSO_RESPONSE),
        ):
            records = alert_sources.fetch_rso_alerts()

        assert len(records) == 2
        first = records[0]
        assert first["source"] == "rso"
        assert first["external_id"] == "23233017"  # stringified from int
        assert first["type"] == "alert"
        assert first["severity"] == "2"
        assert first["area"] == "Podlaskie"
        assert first["title"] == "ALERT RCB - BURZE/2"
        assert first["description"].startswith("Pogoda:")
        assert first["published_at"] == "2026-08-10 14:00:52"
        assert first["valid_from"] == "2026-08-10 13:58:00"
        assert first["valid_to"] == "2026-08-11 04:00:00"
        assert first["url"] is None

    def test_empty_newses(self):
        with patch(
            "modules.clients.alert_sources.requests.get",
            return_value=_mock_response({"pagination": {}, "newses": []}),
        ):
            records = alert_sources.fetch_rso_alerts()
        assert records == []

    def test_missing_provinces_falls_back_to_podlaskie(self):
        item = dict(RSO_RESPONSE["newses"][0])
        item["provinces"] = {}
        with patch(
            "modules.clients.alert_sources.requests.get",
            return_value=_mock_response({"newses": [item]}),
        ):
            records = alert_sources.fetch_rso_alerts()
        assert records[0]["area"] == "Podlaskie"

    def test_custom_wojewodztwo_is_used_in_the_request_url(self):
        """wojewodztwo is the actual portability knob -- a deployment for a
        different region passes its own slug instead of the podlaskie default."""
        with patch(
            "modules.clients.alert_sources.requests.get",
            return_value=_mock_response({"newses": []}),
        ) as mock_get:
            alert_sources.fetch_rso_alerts(wojewodztwo="mazowieckie")
        requested_url = mock_get.call_args[0][0]
        assert "/komunikaty/mazowieckie/" in requested_url

    def test_missing_provinces_falls_back_to_custom_wojewodztwo(self):
        item = dict(RSO_RESPONSE["newses"][0])
        item["provinces"] = {}
        with patch(
            "modules.clients.alert_sources.requests.get",
            return_value=_mock_response({"newses": [item]}),
        ):
            records = alert_sources.fetch_rso_alerts(wojewodztwo="mazowieckie")
        assert records[0]["area"] == "Mazowieckie"


GIOS_AQINDEX_RESPONSE = {
    "@context": {"AqIndex": {"@id": "https://api.gios.gov.pl/pjp-api/v1/rest/aqindex/getIndex"}},
    "AqIndex": {
        "Identyfikator stacji pomiarowej": 11174,
        "Data wykonania obliczeń indeksu": "2026-08-10 18:20:20",
        "Wartość indeksu": 1,
        "Nazwa kategorii indeksu": "Dobry",
        "Wartość indeksu dla wskaźnika SO2": 0,
        "Nazwa kategorii indeksu dla wskażnika SO2": "Bardzo dobry",
    },
}

GIOS_NO_INDEX_RESPONSE = {
    "AqIndex": {
        "Identyfikator stacji pomiarowej": 609,
        "Data wykonania obliczeń indeksu": "2026-08-10 17:20:18",
        "Wartość indeksu": -1,
        "Nazwa kategorii indeksu": "Brak indeksu",
    },
}

PAA_WFS_RESPONSE = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [23.08572, 53.16813]},
            "properties": {
                "id": "4210216f-e980-4aee-afb1-4c69beb1c6d9",
                "stacja": "Białystok",
                "color": "#1dadae",
                "tip_value": "0.064 µSv/h",
                "tip_name": "Białystok",
                "tip_date": "2026-08-04 16:00",
            },
        },
        {
            "geometry": {"type": "Point", "coordinates": [22.948798, 54.130738]},
            "properties": {"id": "f7", "stacja": "Suwałki", "tip_value": "0.084 µSv/h",
                            "tip_date": "2026-08-10 20:00"},
        },
        {
            "geometry": {"type": "Point", "coordinates": [22.88256, 52.40571]},
            "properties": {"id": "4f", "stacja": "Siemiatycze", "tip_value": "0.068 µSv/h",
                            "tip_date": "2026-08-10 20:00"},
        },
        {
            "geometry": {"type": "Point", "coordinates": [21.0, 52.0]},
            "properties": {"id": "aa", "stacja": "Warszawa", "tip_value": "0.070 µSv/h",
                            "tip_date": "2026-08-10 20:00"},
        },
    ],
}


@pytest.mark.unit
class TestFetchGiosAqindex:
    def test_normalizes_category_and_timestamp(self):
        with patch(
            "modules.clients.alert_sources.requests.get",
            return_value=_mock_response(GIOS_AQINDEX_RESPONSE),
        ):
            result = alert_sources.fetch_gios_aqindex(11174)
        assert result == {
            "station_id": 11174,
            "category": "Dobry",
            "computed_at": "2026-08-10 18:20:20",
        }

    def test_returns_none_when_no_index_computed(self):
        """A station without enough sensors reports 'Brak indeksu' -- treat as
        unknown, not as an alertable category."""
        with patch(
            "modules.clients.alert_sources.requests.get",
            return_value=_mock_response(GIOS_NO_INDEX_RESPONSE),
        ):
            result = alert_sources.fetch_gios_aqindex(609)
        assert result is None

    def test_sends_json_ld_accept_header(self):
        """406 Not Acceptable without this header (see research doc gotcha)."""
        with patch(
            "modules.clients.alert_sources.requests.get",
            return_value=_mock_response(GIOS_AQINDEX_RESPONSE),
        ) as mock_get:
            alert_sources.fetch_gios_aqindex(11174)
        assert mock_get.call_args.kwargs["headers"]["Accept"] == "application/ld+json"


@pytest.mark.unit
class TestFetchPaaRadiation:
    def test_filters_to_requested_stations_only(self):
        with patch(
            "modules.clients.alert_sources.requests.get",
            return_value=_mock_response(PAA_WFS_RESPONSE),
        ):
            readings = alert_sources.fetch_paa_radiation(["Bialystok", "Siemiatycze"])
        stations = {r["station"] for r in readings}
        assert stations == {"Białystok", "Siemiatycze"}  # "Warszawa" excluded

    def test_diacritic_and_case_insensitive_matching(self):
        """Config is plain ASCII ('Bialystok', 'Suwalki'); real station names carry
        Polish diacritics ('Białystok', 'Suwałki') -- must match anyway."""
        with patch(
            "modules.clients.alert_sources.requests.get",
            return_value=_mock_response(PAA_WFS_RESPONSE),
        ):
            readings = alert_sources.fetch_paa_radiation(["bialystok", "SUWALKI"])
        stations = {r["station"] for r in readings}
        assert stations == {"Białystok", "Suwałki"}

    def test_parses_value_and_unit_from_combined_string(self):
        with patch(
            "modules.clients.alert_sources.requests.get",
            return_value=_mock_response(PAA_WFS_RESPONSE),
        ):
            readings = alert_sources.fetch_paa_radiation(["Suwalki"])
        assert readings == [{
            "station": "Suwałki", "value": 0.084, "unit": "µSv/h",
            "timestamp": "2026-08-10 20:00",
        }]

    def test_unmatched_station_is_simply_absent(self):
        with patch(
            "modules.clients.alert_sources.requests.get",
            return_value=_mock_response(PAA_WFS_RESPONSE),
        ):
            readings = alert_sources.fetch_paa_radiation(["Nonexistent Station"])
        assert readings == []

    def test_custom_bbox_is_used_in_the_request(self):
        """bbox is the actual portability knob -- a deployment monitoring
        stations outside Podlaskie passes its own box instead of the default."""
        custom_bbox = "14.0,49.0,24.2,55.0,EPSG:4326"
        with patch(
            "modules.clients.alert_sources.requests.get",
            return_value=_mock_response(PAA_WFS_RESPONSE),
        ) as mock_get:
            alert_sources.fetch_paa_radiation(["Suwalki"], bbox=custom_bbox)
        assert mock_get.call_args.kwargs["params"]["bbox"] == custom_bbox

    def test_default_bbox_is_the_podlaskie_box(self):
        with patch(
            "modules.clients.alert_sources.requests.get",
            return_value=_mock_response(PAA_WFS_RESPONSE),
        ) as mock_get:
            alert_sources.fetch_paa_radiation(["Suwalki"])
        assert mock_get.call_args.kwargs["params"]["bbox"] == alert_sources.PAA_PODLASKIE_BBOX
