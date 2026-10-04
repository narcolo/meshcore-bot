#!/usr/bin/env python3
"""Unit tests for the "gwx daily" full-day forecast summary.

Covers the pieces that don't need network access: advice-line priority,
precipitation-window detection, amount formatting, message assembly/
graceful degradation, and the "{cmd:gwx daily}" wiring used by the
scheduled info-channel announcement.
"""

import asyncio
import configparser
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from modules.commands.alternatives.wx_international import GlobalWxCommand
from modules.i18n import Translator


def _build_config() -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    config.add_section("Weather")
    config.set("Weather", "default_city", "Bialystok")
    config.set("Weather", "default_country", "PL")
    config.set("Weather", "temperature_unit", "celsius")
    config.set("Weather", "wind_speed_unit", "kmh")
    config.set("Weather", "precipitation_unit", "mm")
    return config


@pytest.fixture
def cmd(mock_logger):
    bot = Mock()
    bot.logger = mock_logger
    bot.config = _build_config()
    bot.translator = Translator("pl")
    return GlobalWxCommand(bot)


def _mock_message(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        sender_id=None,
        sender_pubkey=None,
        channel="#info",
        is_dm=False,
    )


@pytest.mark.unit
class TestAdviceLinePriority:
    def _advice(self, cmd, **overrides):
        defaults = dict(
            code=3, precip_amount=0, precip_prob=0, hourly_precip=[],
            gust=0, snowfall=0, feels_min=10,
        )
        defaults.update(overrides)
        return cmd._select_advice_line(**defaults)

    def test_thunderstorm_outranks_everything(self, cmd):
        out = self._advice(cmd, code=95, precip_amount=20, gust=80, snowfall=5, feels_min=-10)
        assert out == "⚡ Możliwe burze"

    def test_heavy_rain_from_daily_sum(self, cmd):
        out = self._advice(cmd, code=61, precip_amount=5)
        assert out == "☔ Silny deszcz"

    def test_heavy_rain_from_hourly_spike(self, cmd):
        out = self._advice(cmd, code=61, precip_amount=0.5, hourly_precip=[0, 2.1, 0])
        assert out == "☔ Silny deszcz"

    def test_heavy_rain_outranks_gusts_and_snow(self, cmd):
        out = self._advice(cmd, code=61, precip_amount=10, gust=80, snowfall=5)
        assert out == "☔ Silny deszcz"

    def test_strong_gusts(self, cmd):
        out = self._advice(cmd, code=3, gust=45)
        assert out == "🌬️ Silne porywy"

    def test_gusts_outrank_snow_and_cold(self, cmd):
        out = self._advice(cmd, code=73, gust=50, snowfall=2, feels_min=-5)
        assert out == "🌬️ Silne porywy"

    def test_snow_from_snowfall_amount(self, cmd):
        out = self._advice(cmd, code=3, snowfall=0.5)
        assert out == "❄️ Możliwy śnieg"

    def test_snow_from_weather_code(self, cmd):
        out = self._advice(cmd, code=73, snowfall=0)
        assert out == "❄️ Możliwy śnieg"

    def test_snow_outranks_cold(self, cmd):
        out = self._advice(cmd, code=73, feels_min=-10)
        assert out == "❄️ Możliwy śnieg"

    def test_cold_from_apparent_temp(self, cmd):
        out = self._advice(cmd, feels_min=0)
        assert out == "🧤 Zimno"

    def test_cold_outranks_umbrella(self, cmd):
        out = self._advice(cmd, feels_min=-1, precip_amount=2, precip_prob=80)
        assert out == "🧤 Zimno"

    def test_umbrella_from_amount(self, cmd):
        out = self._advice(cmd, precip_amount=1)
        assert out == "☂️ Weź parasol"

    def test_umbrella_from_probability(self, cmd):
        out = self._advice(cmd, precip_amount=0, precip_prob=50)
        assert out == "☂️ Weź parasol"

    def test_calm_fallback(self, cmd):
        out = self._advice(cmd)
        assert out == "✅ Spokojna pogoda"


@pytest.mark.unit
class TestPrecipitationWindow:
    def test_contiguous_window(self, cmd):
        precip = [0] * 12 + [1, 2, 3, 2, 1, 1] + [0] * 6
        window = cmd._precipitation_window({"precipitation": precip, "precipitation_probability": []})
        assert window == (12, 17)

    def test_probability_only_counts(self, cmd):
        prob = [10] * 10 + [60, 70] + [10] * 12
        window = cmd._precipitation_window({"precipitation": [], "precipitation_probability": prob})
        assert window == (10, 11)

    def test_no_qualifying_hours_returns_none(self, cmd):
        window = cmd._precipitation_window({"precipitation": [0] * 24, "precipitation_probability": [10] * 24})
        assert window is None

    def test_missing_arrays_do_not_crash(self, cmd):
        assert cmd._precipitation_window({}) is None


@pytest.mark.unit
class TestPrecipAmountFormatting:
    def test_small_amount_shows_lt1(self, cmd):
        assert cmd._format_precip_amount(0.3) == "<1mm"

    def test_zero_shows_lt1(self, cmd):
        assert cmd._format_precip_amount(0) == "<1mm"

    def test_whole_mm_amount(self, cmd):
        assert cmd._format_precip_amount(3.2) == "3mm"


@pytest.mark.unit
class TestBuildDailySummaryText:
    def _daily(self, **overrides):
        base = {
            "weather_code": [3],
            "temperature_2m_max": [17],
            "temperature_2m_min": [9],
            "apparent_temperature_max": [15],
            "apparent_temperature_min": [6],
            "precipitation_sum": [3],
            "precipitation_probability_max": [70],
            "wind_speed_10m_max": [18],
            "wind_gusts_10m_max": [38],
            "wind_direction_10m_dominant": [315],  # NW
            "snowfall_sum": [0],
        }
        base.update(overrides)
        return base

    def _hourly(self):
        # Spread thin enough that no single hour crosses the heavy-rain
        # threshold (2mm/h) -- the daily total (3mm) stays an "umbrella"
        # case, matching the spec's worked example.
        return {
            "precipitation": [0] * 12 + [0.5, 0.5, 1, 1, 0.5, 0.5] + [0] * 6,
            "precipitation_probability": [10] * 12 + [60] * 6 + [10] * 6,
        }

    def test_matches_target_format(self, cmd):
        text = cmd._build_daily_summary_text(self._daily(), self._hourly(), "Białystok", max_length=1000)
        expected = (
            "📍 Białystok dziś\n"
            "🌡 9–17°C, odcz. 6–15\n"
            "☁️ pochmurno\n"
            "🌧 70%/3mm 12–17\n"
            "💨 ↖️NW18 G38\n"
            "☂️ Weź parasol"
        )
        assert text == expected

    def test_drops_apparent_temp_first_when_too_long(self, cmd):
        full = cmd._build_daily_summary_text(self._daily(), self._hourly(), "Białystok", max_length=1000)
        budget = cmd._count_display_width(full) - 1
        shortened = cmd._build_daily_summary_text(self._daily(), self._hourly(), "Białystok", max_length=budget)
        assert "odcz." not in shortened
        # The precipitation window is still present once the feels-like clause is gone.
        assert "12–17" in shortened
        assert cmd._count_display_width(shortened) < cmd._count_display_width(full)

    def test_drops_precip_window_when_still_too_long(self, cmd):
        shortened = cmd._build_daily_summary_text(self._daily(), self._hourly(), "Białystok", max_length=40)
        assert "odcz." not in shortened
        assert "12–17" not in shortened
        # The advice line is never dropped.
        assert "Weź parasol" in shortened

    def test_missing_wind_direction_omits_arrow_not_a_wrong_one(self, cmd):
        text = cmd._build_daily_summary_text(
            self._daily(wind_direction_10m_dominant=[None]), self._hourly(), "Białystok", max_length=1000,
        )
        wind_line = text.splitlines()[4]
        assert wind_line == "💨 18 G38"


@pytest.mark.unit
class TestScheduledInvocationWiring:
    def test_bare_daily_uses_default_city_with_daily_forecast_type(self, cmd):
        cmd._get_custom_mqtt_weather_topic = Mock(return_value=None)
        cmd._get_custom_wxsim_source = Mock(return_value=None)
        cmd._get_companion_location = Mock(return_value=None)
        cmd.send_response = AsyncMock(return_value=True)
        cmd.record_execution = Mock()
        cmd.get_weather_for_location = AsyncMock(return_value="ok")

        asyncio.run(cmd.execute(_mock_message("gwx daily")))

        assert cmd.get_weather_for_location.await_count == 1
        args, kwargs = cmd.get_weather_for_location.await_args
        assert args[0] == "Bialystok, PL"
        assert args[1] == "daily"

    def test_explicit_location_daily_suffix(self, cmd):
        cmd._get_custom_mqtt_weather_topic = Mock(return_value=None)
        cmd._get_custom_wxsim_source = Mock(return_value=None)
        cmd.send_response = AsyncMock(return_value=True)
        cmd.record_execution = Mock()
        cmd.get_weather_for_location = AsyncMock(return_value="ok")

        asyncio.run(cmd.execute(_mock_message("gwx Warszawa daily")))

        assert cmd.get_weather_for_location.await_count == 1
        args, kwargs = cmd.get_weather_for_location.await_args
        assert args[0] == "Warszawa"
        assert args[1] == "daily"

    def test_mqtt_source_reports_not_supported_for_daily(self, cmd):
        assert cmd._mqtt_weather_line("some/topic", "daily", None) == (
            "Pełna prognoza dnia nie jest dostępna dla tego źródła pogody"
        )

    def test_wxsim_source_reports_not_supported_for_daily(self, cmd):
        assert cmd._get_wxsim_weather("http://example.com/wxsim.txt", "daily") == (
            "Pełna prognoza dnia nie jest dostępna dla tego źródła pogody"
        )
