"""Unit tests for AlertsService itself: the thin orchestrator layer -- source
discovery (alert_modules/*.py -> AlertSourceBase subclasses) and lifecycle
(start/stop creates one task per enabled source, regardless of which/how many
sources are actually present).

Source-specific behavior (IMGW/RSO dedup, GIOS thresholds, PAA digest/schedule)
lives in tests/unit/alert_modules/ instead -- see test_event_source.py,
test_threshold_source.py, test_base.py, and (once those source modules exist)
test_imgw_meteo.py/test_rso.py/test_gios.py/test_paa.py.
"""

import configparser
import sys
import textwrap
from datetime import datetime, time
from unittest.mock import MagicMock, Mock

import pytest

from modules.service_plugins.alert_modules.base import AlertSourceBase
from modules.service_plugins.alerts_service import AlertsService


class _FakeDBManager:
    def __init__(self):
        self._store: dict[str, str] = {}

    def get_metadata(self, key):
        return self._store.get(key)

    def set_metadata(self, key, value):
        self._store[key] = value


def _bot(db_manager=None, **config_overrides):
    bot = MagicMock()
    bot.logger = Mock()
    # A well-behaved fake by default, not an unconfigured MagicMock attribute:
    # real sources (ImgwMeteoSource, RsoSource, ...) load dedup state from
    # bot.db_manager at construction, and a truthy-but-non-JSON mock there
    # would log a spurious warning on every AlertsService(bot) in this file --
    # and _daily_schedule_loop's persisted-last-slot tests need get/set_metadata
    # to actually round-trip, which a bare `None` can't do either.
    bot.db_manager = db_manager if db_manager is not None else _FakeDBManager()
    config = configparser.ConfigParser()
    config.add_section("Alerts_Service")
    config.set("Alerts_Service", "enabled", "true")
    config.set("Alerts_Service", "channel", "general")
    for key, value in config_overrides.items():
        config.set("Alerts_Service", key, str(value))
    bot.config = config
    return bot


@pytest.mark.unit
class TestDiscovery:
    def test_discovers_only_alertsourcebase_subclasses(self):
        service = AlertsService(_bot())
        for cls in service._discover_source_classes():
            assert issubclass(cls, AlertSourceBase)
            assert cls is not AlertSourceBase

    def test_each_discovered_class_has_a_name(self):
        service = AlertsService(_bot())
        for cls in service._discover_source_classes():
            assert cls.name

    def test_discovery_mechanism_picks_up_a_module_dropped_in_the_package(self, tmp_path, monkeypatch):
        """Exercises the actual pkgutil+inspect discovery mechanism end-to-end
        (not just whatever real sources currently happen to exist), the way
        adding a genuinely new source file is meant to work with zero changes
        to alerts_service.py."""
        module_code = textwrap.dedent("""
            from modules.service_plugins.alert_modules.base import AlertSourceBase

            class ProbeSource(AlertSourceBase):
                name = "probe"
                def is_enabled(self):
                    return True
                async def check(self):
                    pass
        """)
        (tmp_path / "probe_source.py").write_text(module_code)

        import modules.service_plugins.alert_modules as alert_modules_pkg
        monkeypatch.setattr(alert_modules_pkg, "__path__", [str(tmp_path)])
        monkeypatch.delitem(sys.modules, "modules.service_plugins.alert_modules.probe_source", raising=False)

        service = AlertsService(_bot())
        names = {cls.name for cls in service._discover_source_classes()}
        assert "probe" in names

        sys.modules.pop("modules.service_plugins.alert_modules.probe_source", None)


@pytest.mark.unit
class TestLifecycle:
    async def test_start_stop_is_safe_with_no_sources_enabled(self):
        bot = _bot(
            imgw_meteo_enabled="false", rso_enabled="false",
            gios_enabled="false", paa_enabled="false",
        )
        service = AlertsService(bot)
        await service.start()
        try:
            assert service._tasks == []
        finally:
            await service.stop()

    async def test_one_task_per_enabled_source(self):
        """Whatever sources are currently discoverable, exactly the enabled ones
        get a task -- doesn't hardcode a specific count/source, so this test
        keeps working unmodified as sources get added in later branches."""
        bot = _bot()
        service = AlertsService(bot)
        enabled_count = sum(1 for s in service._sources if s.is_enabled())
        await service.start()
        try:
            assert len(service._tasks) == enabled_count
        finally:
            await service.stop()

    # Note: self.enabled defaults True and isn't re-read from config at
    # construction (BaseServicePlugin.__init__ hardcodes it) -- real "disabled"
    # gating happens one level up, in ServicePluginLoader.load_service(), which
    # never constructs the class at all when [Alerts_Service] enabled=false. So
    # there's no meaningful "construct a disabled AlertsService" case to test here.


@pytest.mark.unit
class TestDailyScheduleLoop:
    """_daily_schedule_loop: fixed wall-clock times, not an interval -- fires at
    most once per configured slot per day, restart-safe via a persisted
    last-fired-slot marker. Used by PAA (schedule_kind="daily"); the loop itself
    stays a generic orchestrator primitive, independent of any one source."""

    async def test_fires_for_a_slot_already_passed_today_on_first_run(self, monkeypatch):
        """Startup catch-up: if the bot starts after a slot already passed today
        and it's never fired, fire now rather than waiting until tomorrow."""
        service = AlertsService(_bot())
        fired = []

        async def fake_check():
            fired.append(1)

        async def fake_sleep(seconds):
            service._running = False

        monkeypatch.setattr("modules.service_plugins.alerts_service.asyncio.sleep", fake_sleep)
        service._running = True
        # Midnight has always already passed "today" by construction, regardless
        # of when this test actually runs -- deterministic without freezing time.
        await service._daily_schedule_loop("test", fake_check, [time(0, 0)], "test_slot_key_1")
        assert len(fired) == 1

    async def test_does_not_refire_the_same_slot_twice(self, monkeypatch):
        service = AlertsService(_bot())
        fired = []

        async def fake_check():
            fired.append(1)

        call_count = {"n": 0}

        async def fake_sleep(seconds):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                service._running = False

        monkeypatch.setattr("modules.service_plugins.alerts_service.asyncio.sleep", fake_sleep)
        service._running = True
        await service._daily_schedule_loop("test", fake_check, [time(0, 0)], "test_slot_key_2")
        assert len(fired) == 1  # second loop iteration sees the same already-fired slot

    async def test_restart_with_persisted_slot_does_not_refire(self, monkeypatch):
        db_manager = _FakeDBManager()
        bot = _bot(db_manager=db_manager)
        service = AlertsService(bot)
        # Pre-fire today's midnight slot, simulating a previous process run.
        today_midnight = datetime.combine(datetime.now().date(), time(0, 0))
        service._persist_last_slot("test_slot_key_3", today_midnight.isoformat())

        fired = []

        async def fake_check():
            fired.append(1)

        async def fake_sleep(seconds):
            service._running = False

        monkeypatch.setattr("modules.service_plugins.alerts_service.asyncio.sleep", fake_sleep)
        service._running = True
        await service._daily_schedule_loop("test", fake_check, [time(0, 0)], "test_slot_key_3")
        assert fired == []

    async def test_sleeps_until_next_slot_boundary(self, monkeypatch):
        service = AlertsService(_bot())

        async def fake_check():
            pass

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)
            service._running = False

        monkeypatch.setattr("modules.service_plugins.alerts_service.asyncio.sleep", fake_sleep)
        service._running = True
        await service._daily_schedule_loop(
            "test", fake_check, [time(0, 0), time(23, 59)], "test_slot_key_4"
        )
        assert len(sleep_calls) == 1
        assert 0 < sleep_calls[0] <= 24 * 3600
