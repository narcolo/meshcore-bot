"""Unit tests for flood scope name normalization.

Canonical form is the hash-less display name (e.g. "pl-podlasie"): a leading
'#' in config is accepted and stripped. "name" and "#name" are the same
region — the firmware (RegionMap.cpp, implicit auto hashtag region) and
meshcore-py's set_flood_scope prepend '#' only at key derivation.
"""

from hashlib import sha256

from modules.command_manager import CommandManager


def test_bare_name_kept_hashless():
    assert CommandManager._normalize_scope_name("west") == "west"


def test_hash_prefix_stripped():
    assert CommandManager._normalize_scope_name("#west") == "west"


def test_hashless_production_scope():
    assert CommandManager._normalize_scope_name("pl-podlasie") == "pl-podlasie"


def test_hash_spelling_of_production_scope_stripped():
    assert CommandManager._normalize_scope_name("#pl-podlasie") == "pl-podlasie"


def test_whitespace_stripped():
    assert CommandManager._normalize_scope_name("  #west  ") == "west"


def test_empty_string_is_global():
    assert CommandManager._normalize_scope_name("") == ""


def test_star_is_global():
    assert CommandManager._normalize_scope_name("*") == "*"


def test_zero_is_global():
    assert CommandManager._normalize_scope_name("0") == "0"


def test_none_string_is_global():
    assert CommandManager._normalize_scope_name("None") == "None"


def test_multi_word_name_kept_hashless():
    assert CommandManager._normalize_scope_name("north east") == "north east"


def test_prefixed_multi_word_stripped():
    assert CommandManager._normalize_scope_name("#north east") == "north east"


def test_key_derivation_uses_hash_prefixed_canonical_form():
    """Both spellings of a scope must produce identical key bytes, derived
    from the '#'-prefixed canonical string (firmware/meshcore-py parity)."""
    expected = sha256(b"#pl-podlasie").digest()[:16]
    for spelling in ("pl-podlasie", "#pl-podlasie"):
        name = CommandManager._normalize_scope_name(spelling)
        assert sha256(("#" + name).encode()).digest()[:16] == expected
