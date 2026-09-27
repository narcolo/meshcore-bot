"""Unit tests for flood scope name normalization.

Canonical form is the '#'-prefixed name (e.g. "#pl-podlasie", matching
upstream's modules.flood_scope.normalize_scope_name): a bare name in config
is accepted and gets '#' prepended. "name" and "#name" are the same
region — the firmware (RegionMap.cpp, implicit auto hashtag region) and
meshcore-py's set_flood_scope only prepend '#' when it is absent, so passing
the already-prefixed canonical form through is correct and never double-hashes.
"""

from hashlib import sha256

from modules.command_manager import CommandManager


def test_bare_name_gets_hash_prefixed():
    assert CommandManager._normalize_scope_name("west") == "#west"


def test_hash_prefix_kept():
    assert CommandManager._normalize_scope_name("#west") == "#west"


def test_bare_production_scope_gets_hash_prefixed():
    assert CommandManager._normalize_scope_name("pl-podlasie") == "#pl-podlasie"


def test_hash_spelling_of_production_scope_kept():
    assert CommandManager._normalize_scope_name("#pl-podlasie") == "#pl-podlasie"


def test_whitespace_stripped():
    assert CommandManager._normalize_scope_name("  #west  ") == "#west"


def test_whitespace_stripped_bare_name():
    assert CommandManager._normalize_scope_name("  west  ") == "#west"


def test_empty_string_is_global():
    assert CommandManager._normalize_scope_name("") == ""


def test_star_is_global():
    assert CommandManager._normalize_scope_name("*") == "*"


def test_zero_is_global():
    assert CommandManager._normalize_scope_name("0") == "0"


def test_none_string_is_global():
    assert CommandManager._normalize_scope_name("None") == "None"


def test_multi_word_name_gets_hash_prefixed():
    assert CommandManager._normalize_scope_name("north east") == "#north east"


def test_prefixed_multi_word_kept():
    assert CommandManager._normalize_scope_name("#north east") == "#north east"


def test_key_derivation_uses_hash_prefixed_canonical_form():
    """Both spellings of a scope must produce identical key bytes, derived
    directly from the '#'-prefixed canonical string (firmware/meshcore-py parity).
    The canonical form already carries the '#', so hashing it directly (not
    "#" + name) is what avoids a double-hash.
    """
    expected = sha256(b"#pl-podlasie").digest()[:16]
    for spelling in ("pl-podlasie", "#pl-podlasie"):
        name = CommandManager._normalize_scope_name(spelling)
        assert name == "#pl-podlasie"
        assert sha256(name.encode()).digest()[:16] == expected


def test_normalization_is_idempotent():
    """Re-normalizing an already-canonical value must not double-hash it."""
    once = CommandManager._normalize_scope_name("west")
    twice = CommandManager._normalize_scope_name(once)
    assert once == twice == "#west"
