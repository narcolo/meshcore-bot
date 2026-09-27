"""Tests for modules.daily_public_notice: [Daily_Public_Notice] config parsing
and {scopes}/byte-budget message rendering (pure logic, no APScheduler)."""

from configparser import ConfigParser
from unittest.mock import Mock

from modules.daily_public_notice import (
    DailyPublicNoticeConfig,
    load_config,
    render_messages,
)


def _config(**overrides) -> ConfigParser:
    """A [Daily_Public_Notice] config with the documented defaults, overridable."""
    values = {
        "enabled": "true",
        "time": "19:00",
        "channel": "Public",
        "flood_scope": "pl-podlasie",
        "recommended_scopes": "pl,pl-podlasie,pl-bia",
        "max_message_bytes": "120",
        "message_delay_seconds": "5",
        "message.1": "Nowy w MeshCore? Ustaw scope: {scopes}.",
        "message.2": "Więcej informacji: https://meshcore.podlasie.pl",
    }
    values.update(overrides)
    cfg = ConfigParser()
    cfg.add_section("Daily_Public_Notice")
    for key, value in values.items():
        if value is None:
            continue
        cfg.set("Daily_Public_Notice", key, value)
    return cfg


class TestLoadConfig:
    def test_missing_section_returns_none(self):
        cfg = ConfigParser()
        assert load_config(cfg) is None

    def test_disabled_returns_none(self):
        cfg = _config(enabled="false")
        assert load_config(cfg) is None

    def test_default_values_parsed(self):
        cfg = _config()
        result = load_config(cfg)
        assert isinstance(result, DailyPublicNoticeConfig)
        assert result.hour == 19
        assert result.minute == 0
        assert result.channel == "Public"
        assert result.flood_scope == "pl-podlasie"
        assert result.recommended_scopes == ("pl", "pl-podlasie", "pl-bia")
        assert result.max_message_bytes == 120
        assert result.message_delay_seconds == 5.0
        assert result.message_templates == (
            "Nowy w MeshCore? Ustaw scope: {scopes}.",
            "Więcej informacji: https://meshcore.podlasie.pl",
        )

    def test_custom_time(self):
        result = load_config(_config(time="07:30"))
        assert (result.hour, result.minute) == (7, 30)

    def test_custom_channel(self):
        result = load_config(_config(channel="general"))
        assert result.channel == "general"

    def test_custom_flood_scope(self):
        result = load_config(_config(**{"flood_scope": "sea"}))
        assert result.flood_scope == "sea"

    def test_custom_recommended_scopes(self):
        result = load_config(_config(**{"recommended_scopes": "a,b"}))
        assert result.recommended_scopes == ("a", "b")

    def test_custom_delay(self):
        result = load_config(_config(**{"message_delay_seconds": "10"}))
        assert result.message_delay_seconds == 10.0

    def test_custom_byte_limit(self):
        result = load_config(_config(**{"max_message_bytes": "200"}))
        assert result.max_message_bytes == 200

    def test_arbitrary_ordered_message_entries(self):
        cfg = _config()
        cfg.set("Daily_Public_Notice", "message.3", "Third message")
        cfg.set("Daily_Public_Notice", "message.10", "Tenth message")
        result = load_config(cfg)
        assert result.message_templates == (
            "Nowy w MeshCore? Ustaw scope: {scopes}.",
            "Więcej informacji: https://meshcore.podlasie.pl",
            "Third message",
            "Tenth message",
        )

    def test_numeric_ordering_not_lexical(self):
        """message.10 must sort after message.2, not before it."""
        cfg = ConfigParser()
        cfg.add_section("Daily_Public_Notice")
        cfg.set("Daily_Public_Notice", "enabled", "true")
        cfg.set("Daily_Public_Notice", "message.2", "second")
        cfg.set("Daily_Public_Notice", "message.10", "tenth")
        result = load_config(cfg)
        assert result.message_templates == ("second", "tenth")

    def test_invalid_time_disables_and_logs(self):
        logger = Mock()
        result = load_config(_config(time="not-a-time"), logger=logger)
        assert result is None
        logger.error.assert_called_once()

    def test_empty_time_disables(self):
        assert load_config(_config(time="")) is None

    def test_out_of_range_time_disables(self):
        assert load_config(_config(time="24:00")) is None
        assert load_config(_config(time="19:60")) is None

    def test_empty_channel_disables_and_logs(self):
        logger = Mock()
        result = load_config(_config(channel=""), logger=logger)
        assert result is None
        logger.error.assert_called_once()

    def test_negative_delay_disables(self):
        assert load_config(_config(**{"message_delay_seconds": "-1"})) is None

    def test_zero_delay_is_valid(self):
        result = load_config(_config(**{"message_delay_seconds": "0"}))
        assert result.message_delay_seconds == 0.0

    def test_non_positive_byte_limit_disables(self):
        assert load_config(_config(**{"max_message_bytes": "0"})) is None
        assert load_config(_config(**{"max_message_bytes": "-5"})) is None

    def test_no_message_entries_disables_and_logs(self):
        cfg = ConfigParser()
        cfg.add_section("Daily_Public_Notice")
        cfg.set("Daily_Public_Notice", "enabled", "true")
        logger = Mock()
        result = load_config(cfg, logger=logger)
        assert result is None
        logger.error.assert_called_once()


class TestRenderMessages:
    def _cfg(self, **kwargs) -> DailyPublicNoticeConfig:
        defaults = dict(
            hour=19, minute=0, channel="Public", flood_scope="pl-podlasie",
            recommended_scopes=("pl", "pl-podlasie", "pl-bia"),
            max_message_bytes=120, message_delay_seconds=5.0,
            message_templates=("Nowy w MeshCore? Ustaw scope: {scopes}.",),
        )
        defaults.update(kwargs)
        return DailyPublicNoticeConfig(**defaults)

    def test_scopes_rendered_in_configured_order(self):
        rendered = render_messages(self._cfg())
        assert rendered == ["Nowy w MeshCore? Ustaw scope: pl, pl-podlasie, pl-bia."]

    def test_message_without_placeholder_passes_through(self):
        cfg = self._cfg(message_templates=("Więcej informacji: https://meshcore.podlasie.pl",))
        rendered = render_messages(cfg)
        assert rendered == ["Więcej informacji: https://meshcore.podlasie.pl"]

    def test_multiple_messages_all_rendered_in_order(self):
        cfg = self._cfg(message_templates=(
            "Ustaw scope: {scopes}.",
            "Więcej informacji: https://meshcore.podlasie.pl",
        ))
        rendered = render_messages(cfg)
        assert rendered == [
            "Ustaw scope: pl, pl-podlasie, pl-bia.",
            "Więcej informacji: https://meshcore.podlasie.pl",
        ]

    def test_trailing_scopes_dropped_one_at_a_time_to_fit(self):
        # Budget fits "pl, pl-podlasie" plus the fixed text, but not "pl-bia" too.
        template = "Ustaw scope: {scopes}."
        fits_two = f"Ustaw scope: {'pl, pl-podlasie'}."
        cfg = self._cfg(
            recommended_scopes=("pl", "pl-podlasie", "pl-bia"),
            max_message_bytes=len(fits_two.encode("utf-8")),
            message_templates=(template,),
        )
        rendered = render_messages(cfg)
        assert rendered == ["Ustaw scope: pl, pl-podlasie."]

    def test_leading_scopes_have_highest_priority(self):
        # Budget only fits the single highest-priority scope.
        template = "S: {scopes}."
        cfg = self._cfg(
            recommended_scopes=("pl", "pl-podlasie", "pl-bia"),
            max_message_bytes=len("S: pl.".encode("utf-8")),
            message_templates=(template,),
        )
        rendered = render_messages(cfg)
        assert rendered == ["S: pl."]

    def test_utf8_byte_length_not_character_count(self):
        # "Więcej" has multibyte characters (ę=2 bytes, ó would too); a
        # char-count check would under-count and wrongly consider it short enough.
        template = "Więcej informacji: https://meshcore.podlasie.pl"
        byte_len = len(template.encode("utf-8"))
        char_len = len(template)
        assert byte_len > char_len  # sanity: multibyte chars are present
        cfg = self._cfg(max_message_bytes=byte_len, message_templates=(template,))
        assert render_messages(cfg) == [template]
        cfg_too_small = self._cfg(max_message_bytes=byte_len - 1, message_templates=(template,))
        assert render_messages(cfg_too_small) is None

    def test_url_never_shortened_or_truncated(self):
        url = "https://meshcore.podlasie.pl"
        cfg = self._cfg(
            max_message_bytes=len(f"Info: {url}".encode("utf-8")),
            message_templates=(f"Info: {url}",),
        )
        rendered = render_messages(cfg)
        assert rendered == [f"Info: {url}"]
        assert url in rendered[0]

    def test_oversized_static_message_returns_none_and_logs(self):
        cfg = self._cfg(
            max_message_bytes=10,
            message_templates=("This message has no {scopes} placeholder and is long",),
        )
        logger = Mock()
        result = render_messages(cfg, logger=logger)
        assert result is None
        logger.error.assert_called_once()
        args = logger.error.call_args.args
        assert args[1] == 1  # message.1
        assert args[3] == 10  # configured limit

    def test_oversized_even_with_zero_scopes_returns_none(self):
        template = "This fixed prefix text alone already exceeds the limit {scopes}"
        cfg = self._cfg(
            recommended_scopes=("pl", "pl-podlasie", "pl-bia"),
            max_message_bytes=5,
            message_templates=(template,),
        )
        assert render_messages(cfg) is None

    def test_skips_entire_batch_when_any_message_fails(self):
        """A valid first message must not be sent if a later one is invalid."""
        cfg = self._cfg(
            max_message_bytes=200,
            message_templates=(
                "Ustaw scope: {scopes}.",
                "x" * 500,  # far too long, no placeholder
            ),
        )
        assert render_messages(cfg) is None
