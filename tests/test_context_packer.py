"""Tests for the context-packer seed.

One test per documented behavior in docs/seeds/context-packer.md, plus the edge
cases the boundary names: caps, error paths, empty input, unicode, opaque keys,
and purity. Pure module: no stubs, no fixtures, no I/O.
"""

from __future__ import annotations

import dataclasses

import pytest

from brief.lib.context_packer import (
    DEFAULT_CHARS_PER_TOKEN,
    DEFAULT_OUTPUT_RESERVE_TOKENS,
    DEFAULT_TRUNCATION_MARKER,
    PackResult,
    cap_text,
    context_budget_chars,
    pack_items,
)


# --------------------------------------------------------------------------
# context_budget_chars
# --------------------------------------------------------------------------


def test_budget_is_window_minus_reserve_times_chars_per_token_minus_overhead():
    # (8000 - 4096) * 4 - 1000
    assert context_budget_chars(8000, prompt_overhead_chars=1000) == 14616


def test_budget_default_chars_per_token_is_four():
    assert DEFAULT_CHARS_PER_TOKEN == 4
    assert context_budget_chars(5000, output_reserve_tokens=0) == 20000


def test_budget_honors_non_default_chars_per_token():
    assert (
        context_budget_chars(1000, chars_per_token=3, output_reserve_tokens=0) == 3000
    )


def test_budget_default_output_reserve_is_4096_tokens():
    assert context_budget_chars(4096) == 0
    assert context_budget_chars(4196) == 400


def test_budget_floored_at_zero_when_reserve_exhausts_window():
    assert context_budget_chars(1024) == 0


def test_budget_floored_at_zero_when_overhead_exhausts_window():
    assert (
        context_budget_chars(5000, output_reserve_tokens=0, prompt_overhead_chars=10**9)
        == 0
    )


def test_budget_zero_window_is_zero_not_negative():
    assert context_budget_chars(0) == 0


def test_budget_rejects_non_positive_chars_per_token():
    with pytest.raises(ValueError):
        context_budget_chars(8000, chars_per_token=0)


def test_budget_rejects_negative_prompt_overhead():
    with pytest.raises(ValueError):
        context_budget_chars(8000, prompt_overhead_chars=-1)


def test_budget_rejects_negative_output_reserve():
    with pytest.raises(ValueError):
        context_budget_chars(8000, output_reserve_tokens=-1)


def test_public_defaults_are_the_documented_values():
    assert DEFAULT_CHARS_PER_TOKEN == 4
    assert DEFAULT_OUTPUT_RESERVE_TOKENS == 4096
    assert DEFAULT_TRUNCATION_MARKER == "…"
    assert context_budget_chars(10_000) == (
        (10_000 - DEFAULT_OUTPUT_RESERVE_TOKENS) * DEFAULT_CHARS_PER_TOKEN
    )


# --------------------------------------------------------------------------
# cap_text
# --------------------------------------------------------------------------


def test_cap_text_leaves_short_text_untouched():
    assert cap_text("hello", 10) == ("hello", False)


def test_cap_text_leaves_exactly_sized_text_untouched():
    assert cap_text("hello", 5) == ("hello", False)


def test_cap_text_marker_counts_toward_max_chars():
    capped, was_capped = cap_text("abcdefghij", 5)
    assert was_capped is True
    assert capped == "abcd…"
    assert len(capped) == 5


def test_cap_text_uses_a_custom_marker():
    capped, was_capped = cap_text("abcdefghij", 6, marker="[cut]")
    assert (capped, was_capped) == ("a[cut]", True)


def test_cap_text_empty_marker_is_a_plain_cut():
    assert cap_text("abcdefghij", 4, marker="") == ("abcd", True)


def test_cap_text_drops_marker_when_cap_cannot_hold_it():
    capped, was_capped = cap_text("abcdefghij", 3, marker="[cut]")
    assert (capped, was_capped) == ("abc", True)
    assert len(capped) == 3


def test_cap_text_zero_cap_yields_empty_string():
    assert cap_text("abc", 0) == ("", True)


def test_cap_text_negative_cap_yields_empty_string():
    assert cap_text("abc", -5) == ("", True)


def test_cap_text_empty_text_is_never_capped():
    assert cap_text("", 0) == ("", False)


def test_cap_text_empty_text_is_never_capped_even_under_a_negative_cap():
    # The flag means "characters were removed"; nothing was removed from "".
    assert cap_text("", -5) == ("", False)


def test_cap_text_rejects_non_string_text():
    # A list has len() and slices, so without an explicit type check this would
    # sail through and hand the caller back a list instead of text.
    with pytest.raises(TypeError):
        cap_text(["a", "b"], 10)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        cap_text(123, 10)  # type: ignore[arg-type]


def test_cap_text_counts_code_points_not_bytes():
    # Four CJK chars are 12 UTF-8 bytes but 4 characters.
    text = "日本語訳"
    assert cap_text(text, 4) == (text, False)
    capped, was_capped = cap_text(text, 3)
    assert (capped, was_capped) == ("日本…", True)
    assert len(capped) == 3


def test_cap_text_does_not_split_astral_characters():
    text = "🐍🐍🐍🐍"
    capped, was_capped = cap_text(text, 3)
    assert (capped, was_capped) == ("🐍🐍…", True)
    # Each astral char survives whole: 2 snakes, not 4 half surrogates.
    assert capped.count("🐍") == 2
    assert len(capped) == 3


# --------------------------------------------------------------------------
# pack_items — the happy path and the prefix rule
# --------------------------------------------------------------------------


def test_pack_everything_fits():
    items = [(1, "aaa"), (2, "bbb"), (3, "ccc")]
    result = pack_items(items, budget_chars=100)
    assert result.packed == items
    assert result.left_out == []
    assert result.truncated == []
    assert result.chars_used == 9
    assert result.budget_chars == 100


def test_pack_stops_at_first_item_that_does_not_fit():
    items = [(1, "a" * 4), (2, "b" * 4), (3, "c" * 4)]
    result = pack_items(items, budget_chars=9)
    assert result.packed == [(1, "aaaa"), (2, "bbbb")]
    assert result.left_out == [3]
    assert result.chars_used == 8


def test_pack_does_not_skip_ahead_to_a_smaller_lower_ranked_item():
    # Rank order wins: the tiny item 3 would fit, but 2 did not, so 2 and 3 are out.
    items = [(1, "a"), (2, "b" * 50), (3, "c")]
    result = pack_items(items, budget_chars=10)
    assert result.packed == [(1, "a")]
    assert result.left_out == [2, 3]
    assert result.chars_used == 1


def test_pack_left_out_keeps_input_order():
    items = [(k, "x" * 10) for k in ("a", "b", "c", "d")]
    result = pack_items(items, budget_chars=10)
    assert result.packed == [("a", "x" * 10)]
    assert result.left_out == ["b", "c", "d"]


def test_pack_fills_the_budget_exactly():
    items = [(1, "a" * 5), (2, "b" * 5)]
    result = pack_items(items, budget_chars=10)
    assert result.packed == items
    assert result.chars_used == 10


# --------------------------------------------------------------------------
# pack_items — overhead, max_items, caps
# --------------------------------------------------------------------------


def test_pack_counts_per_item_overhead_against_the_budget():
    items = [(1, "aaa"), (2, "bbb"), (3, "ccc")]
    result = pack_items(items, budget_chars=10, per_item_overhead_chars=2)
    assert result.packed == [(1, "aaa"), (2, "bbb")]
    assert result.left_out == [3]
    assert result.chars_used == 10


def test_pack_honors_max_items_even_when_budget_remains():
    items = [(1, "a"), (2, "b"), (3, "c")]
    result = pack_items(items, budget_chars=1000, max_items=2)
    assert result.packed == [(1, "a"), (2, "b")]
    assert result.left_out == [3]


def test_pack_max_items_zero_packs_nothing():
    items = [(1, "a"), (2, "b")]
    result = pack_items(items, budget_chars=1000, max_items=0)
    assert result.packed == []
    assert result.left_out == [1, 2]
    assert result.chars_used == 0


def test_pack_caps_each_item_before_fitting_and_reports_truncated_keys():
    items = [(1, "a" * 100), (2, "b" * 3)]
    result = pack_items(items, budget_chars=1000, max_item_chars=10)
    assert result.packed == [(1, "a" * 9 + "…"), (2, "bbb")]
    assert result.truncated == [1]
    assert result.chars_used == 13


def test_pack_capping_makes_room_that_the_uncapped_text_would_not_have():
    items = [(1, "a" * 100), (2, "b" * 100)]
    result = pack_items(items, budget_chars=20, max_item_chars=10)
    assert [key for key, _ in result.packed] == [1, 2]
    assert result.truncated == [1, 2]
    assert result.chars_used == 20


def test_pack_uses_a_custom_truncation_marker():
    items = [(1, "a" * 100)]
    result = pack_items(
        items, budget_chars=100, max_item_chars=10, truncation_marker=">>"
    )
    assert result.packed == [(1, "a" * 8 + ">>")]
    assert result.truncated == [1]


def test_pack_packed_text_is_the_capped_text_not_the_original():
    original = "b" * 100
    items = [(1, original)]
    result = pack_items(items, budget_chars=1000, max_item_chars=10)
    ((_, packed_text),) = result.packed
    assert packed_text != original
    assert len(packed_text) == 10


# --------------------------------------------------------------------------
# pack_items — items that cannot fit at all
# --------------------------------------------------------------------------


def test_pack_item_larger_than_the_whole_budget_is_left_out_not_partially_packed():
    items = [(1, "a" * 50)]
    result = pack_items(items, budget_chars=10)
    assert result.packed == []
    assert result.left_out == [1]
    assert result.chars_used == 0


def test_pack_capped_but_still_too_large_item_is_not_reported_as_truncated():
    # Cap 20, budget 5: item 1 is capped then rejected; nothing about it reached the model.
    items = [(1, "a" * 100), (2, "b")]
    result = pack_items(items, budget_chars=5, max_item_chars=20)
    assert result.packed == []
    assert result.left_out == [1, 2]
    assert result.truncated == []


def test_pack_zero_budget_leaves_everything_out():
    items = [(1, "a"), (2, "b")]
    result = pack_items(items, budget_chars=0)
    assert result.packed == []
    assert result.left_out == [1, 2]
    assert result.chars_used == 0
    assert result.budget_chars == 0


def test_pack_empty_items_gives_an_empty_result():
    result = pack_items([], budget_chars=100)
    assert result == PackResult(
        packed=[], left_out=[], truncated=[], chars_used=0, budget_chars=100
    )


def test_pack_empty_text_still_costs_its_overhead():
    items = [(1, ""), (2, ""), (3, "")]
    result = pack_items(items, budget_chars=2, per_item_overhead_chars=1)
    assert result.packed == [(1, ""), (2, "")]
    assert result.left_out == [3]
    assert result.chars_used == 2


# --------------------------------------------------------------------------
# pack_items — keys, unicode, purity, error paths
# --------------------------------------------------------------------------


def test_pack_keys_are_opaque_and_returned_as_given():
    items = [("src", "a"), (("tuple", 2), "b"), (None, "c")]
    result = pack_items(items, budget_chars=2)
    assert result.packed == [("src", "a"), (("tuple", 2), "b")]
    assert result.left_out == [None]


def test_pack_measures_unicode_text_in_code_points():
    items = [(1, "héllo wörld"), (2, "日本語")]
    result = pack_items(items, budget_chars=11)
    assert result.packed == [(1, "héllo wörld")]
    assert result.left_out == [2]
    assert result.chars_used == 11


def test_pack_accepts_any_sequence_and_does_not_mutate_it():
    items = ((1, "a"), (2, "b" * 99))
    snapshot = tuple(items)
    result = pack_items(items, budget_chars=5)
    assert items == snapshot
    assert result.packed == [(1, "a")]


def test_pack_is_deterministic():
    items = [(k, "x" * k) for k in range(1, 10)]
    first = pack_items(
        items, budget_chars=17, max_item_chars=5, per_item_overhead_chars=1
    )
    second = pack_items(
        items, budget_chars=17, max_item_chars=5, per_item_overhead_chars=1
    )
    assert first == second


def test_pack_result_is_frozen():
    result = pack_items([(1, "a")], budget_chars=10)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.chars_used = 99  # type: ignore[misc]


def test_pack_rejects_negative_per_item_overhead():
    with pytest.raises(ValueError):
        pack_items([(1, "a")], budget_chars=10, per_item_overhead_chars=-1)


def test_pack_rejects_negative_max_items():
    with pytest.raises(ValueError):
        pack_items([(1, "a")], budget_chars=10, max_items=-1)


def test_pack_rejects_negative_max_item_chars():
    with pytest.raises(ValueError):
        pack_items([(1, "a")], budget_chars=10, max_item_chars=-1)


def test_pack_rejects_malformed_items():
    with pytest.raises((TypeError, ValueError)):
        pack_items([(1, "ok"), (2,)], budget_chars=100)  # type: ignore[list-item]


def test_pack_rejects_non_string_text():
    with pytest.raises(TypeError):
        pack_items([(1, ["a", "b"])], budget_chars=100)  # type: ignore[list-item]
    with pytest.raises(TypeError):
        pack_items([(1, 123)], budget_chars=100)  # type: ignore[list-item]


def test_pack_rejects_non_string_text_on_the_capped_path_too():
    # max_item_chars routes text through cap_text; that path must type-check too.
    with pytest.raises(TypeError):
        pack_items([(1, ["a", "b"])], budget_chars=100, max_item_chars=10)  # type: ignore[list-item]


def test_pack_negative_budget_packs_nothing_and_echoes_the_budget():
    result = pack_items([(1, ""), (2, "a")], budget_chars=-5)
    assert result.packed == []
    assert result.left_out == [1, 2]
    assert result.chars_used == 0
    assert result.budget_chars == -5
