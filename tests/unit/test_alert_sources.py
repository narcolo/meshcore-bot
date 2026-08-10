"""Unit tests for modules/clients/alert_sources.py (Phase 2a alert fetch/normalize).

Fixture payloads are trimmed real examples captured during the source research in
plans/alert-feeds-research.md -- no network calls are made (requests.get is mocked).
"""

from unittest.mock import Mock, patch

import pytest

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
    resp.json.return_value = payload
    resp.raise_for_status = Mock()
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


@pytest.mark.unit
class TestFetchRsoPodlaskie:
    def test_normalizes_all_fields(self):
        with patch(
            "modules.clients.alert_sources.requests.get",
            return_value=_mock_response(RSO_RESPONSE),
        ):
            records = alert_sources.fetch_rso_podlaskie()

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
            records = alert_sources.fetch_rso_podlaskie()
        assert records == []

    def test_missing_provinces_falls_back_to_podlaskie(self):
        item = dict(RSO_RESPONSE["newses"][0])
        item["provinces"] = {}
        with patch(
            "modules.clients.alert_sources.requests.get",
            return_value=_mock_response({"newses": [item]}),
        ):
            records = alert_sources.fetch_rso_podlaskie()
        assert records[0]["area"] == "Podlaskie"
