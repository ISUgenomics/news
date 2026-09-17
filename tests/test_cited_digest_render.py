"""Tests for the cited-digest-render seed.

One test per documented behavior in docs/seeds/cited-digest-render.md, plus the edge cases
named there: no citations, unknown ids, empty results, caps on nothing, unicode, malformed
input, duplicate ids, custom extra sections. Pure functions, no fixtures, no network.
"""

from __future__ import annotations

import pytest

from brief.lib.cited_digest_render import (
    DEFAULT_EXTRA_SECTIONS,
    DroppedEntry,
    RenderResult,
    enforce_citations,
    render_digest,
    render_markdown,
)

URLS = {
    11: "https://example.edu/a",
    12: "https://example.edu/b",
    13: "https://example.edu/c",
}


def _result():
    return {
        "buckets": [
            {
                "name": "Funding",
                "entries": [{"text": "A grant landed.", "item_ids": [11, 12]}],
            },
            {
                "name": "Papers",
                "entries": [{"text": "A paper appeared.", "item_ids": [13]}],
            },
        ],
        "watch_list": [{"text": "Something to watch.", "item_ids": [12]}],
    }


# --- the whole page ---------------------------------------------------------


def test_render_digest_produces_the_documented_page_shape():
    out = render_digest(
        _result(),
        URLS,
        title="Weekly digest",
        subtitle="Week of 2026-09-14",
        footer="Generated from 3 of 40 candidates.",
    )
    assert isinstance(out, RenderResult)
    assert out.kept == 3
    assert out.dropped == []
    assert out.markdown == (
        "# Weekly digest\n"
        "\n"
        "Week of 2026-09-14\n"
        "\n"
        "## Funding\n"
        "\n"
        "- A grant landed. [11](https://example.edu/a) [12](https://example.edu/b)\n"
        "\n"
        "## Papers\n"
        "\n"
        "- A paper appeared. [13](https://example.edu/c)\n"
        "\n"
        "## Watch list\n"
        "\n"
        "- Something to watch. [12](https://example.edu/b)\n"
        "\n"
        "Generated from 3 of 40 candidates.\n"
    )


def test_subtitle_is_omitted_when_empty():
    md = render_digest(_result(), URLS, title="T").markdown
    assert md.startswith("# T\n\n## Funding\n")


def test_footer_is_placed_verbatim_and_last():
    footer = "Sources: 2 feeds.  Provider: stub."
    md = render_digest(_result(), URLS, title="T", footer=footer).markdown
    assert md.endswith("\n\n" + footer + "\n")


def test_footer_is_omitted_when_empty():
    md = render_digest(_result(), URLS, title="T").markdown
    assert md.endswith("- Something to watch. [12](https://example.edu/b)\n")


# --- ordering ---------------------------------------------------------------


def test_bucket_order_leads_and_unnamed_buckets_follow_in_model_order():
    result = {
        "buckets": [
            {"name": "Papers", "entries": [{"text": "p", "item_ids": [11]}]},
            {"name": "Funding", "entries": [{"text": "f", "item_ids": [11]}]},
            {"name": "Events", "entries": [{"text": "e", "item_ids": [11]}]},
        ]
    }
    md = render_digest(result, URLS, title="T", bucket_order=["Funding"]).markdown
    assert [line for line in md.splitlines() if line.startswith("## ")] == [
        "## Funding",
        "## Papers",
        "## Events",
    ]


def test_bucket_order_naming_a_bucket_the_model_did_not_return_is_ignored():
    md = render_digest(
        _result(), URLS, title="T", bucket_order=["Ghost", "Papers"]
    ).markdown
    assert [line for line in md.splitlines() if line.startswith("## ")] == [
        "## Papers",
        "## Funding",
        "## Watch list",
    ]


def test_extra_sections_render_after_every_bucket():
    md = render_digest(_result(), URLS, title="T").markdown
    assert md.index("## Watch list") > md.index("## Papers")


# --- the citation rule ------------------------------------------------------


def test_entry_with_no_item_ids_is_dropped_and_reported():
    result = {
        "buckets": [
            {
                "name": "Funding",
                "entries": [
                    {"text": "cited", "item_ids": [11]},
                    {"text": "uncited", "item_ids": []},
                ],
            }
        ]
    }
    out = render_digest(result, URLS, title="T")
    assert "uncited" not in out.markdown
    assert out.kept == 1
    assert out.dropped == [
        DroppedEntry(
            section="Funding", text="uncited", item_ids=[], reason="no_ids", unknown=()
        )
    ]


def test_entry_missing_the_item_ids_key_is_dropped_as_no_ids():
    result = {"buckets": [{"name": "Funding", "entries": [{"text": "bare"}]}]}
    out = render_digest(result, URLS, title="T")
    assert out.markdown == ""
    assert out.dropped[0].reason == "no_ids"
    assert out.dropped[0].item_ids == []


def test_entry_citing_an_unknown_id_is_dropped_and_names_the_offender():
    result = {
        "buckets": [
            {"name": "Funding", "entries": [{"text": "invented", "item_ids": [99]}]}
        ]
    }
    out = render_digest(result, URLS, title="T")
    assert out.markdown == ""
    assert out.dropped == [
        DroppedEntry(
            section="Funding",
            text="invented",
            item_ids=[99],
            reason="unknown_id",
            unknown=(99,),
        )
    ]


def test_one_bad_id_drops_the_whole_entry_even_beside_a_real_one():
    result = {
        "buckets": [
            {
                "name": "Funding",
                "entries": [{"text": "half true", "item_ids": [11, 99]}],
            }
        ]
    }
    out = render_digest(result, URLS, title="T")
    assert out.markdown == ""
    assert out.kept == 0
    assert out.dropped[0].reason == "unknown_id"
    assert out.dropped[0].unknown == (99,)
    assert out.dropped[0].item_ids == [11, 99]


def test_unknown_ids_are_reported_once_each_in_first_seen_order():
    result = {
        "buckets": [
            {"name": "F", "entries": [{"text": "x", "item_ids": [98, 11, 99, 98]}]}
        ]
    }
    _, dropped = enforce_citations(result, URLS.keys())
    assert dropped[0].unknown == (98, 99)


def test_a_string_id_is_not_the_integer_id_and_is_dropped_not_coerced():
    result = {
        "buckets": [{"name": "F", "entries": [{"text": "x", "item_ids": ["12"]}]}]
    }
    out = render_digest(result, URLS, title="T")
    assert out.markdown == ""
    assert out.dropped[0].reason == "unknown_id"
    assert out.dropped[0].unknown == ("12",)


def test_bucket_left_empty_after_drops_is_omitted_from_the_page():
    result = {
        "buckets": [
            {"name": "Funding", "entries": [{"text": "ok", "item_ids": [11]}]},
            {"name": "Papers", "entries": [{"text": "bad", "item_ids": [99]}]},
        ]
    }
    md = render_digest(result, URLS, title="T").markdown
    assert "## Papers" not in md
    assert "## Funding" in md


def test_markdown_is_empty_when_nothing_survives():
    result = {
        "buckets": [
            {"name": "Funding", "entries": [{"text": "bad", "item_ids": [99]}]}
        ],
        "watch_list": [{"text": "also bad", "item_ids": []}],
    }
    out = render_digest(result, URLS, title="T", footer="f")
    assert out.markdown == ""
    assert out.kept == 0
    assert len(out.dropped) == 2


def test_drop_from_an_extra_section_is_reported_under_its_heading():
    result = {"watch_list": [{"text": "w", "item_ids": []}]}
    out = render_digest(result, URLS, title="T")
    assert out.dropped[0].section == "Watch list"


# --- enforce_citations on its own -------------------------------------------


def test_enforce_citations_returns_a_clean_structure_without_empty_buckets():
    result = {
        "buckets": [
            {
                "name": "Funding",
                "entries": [
                    {"text": "ok", "item_ids": [11]},
                    {"text": "bad", "item_ids": [99]},
                ],
            },
            {"name": "Papers", "entries": [{"text": "bad", "item_ids": []}]},
        ],
        "watch_list": [{"text": "ok", "item_ids": [12]}],
    }
    clean, dropped = enforce_citations(result, URLS.keys())
    assert clean == {
        "buckets": [{"name": "Funding", "entries": [{"text": "ok", "item_ids": [11]}]}],
        "watch_list": [{"text": "ok", "item_ids": [12]}],
    }
    assert [d.reason for d in dropped] == ["unknown_id", "no_ids"]


def test_enforce_citations_does_not_mutate_the_input():
    result = _result()
    result["buckets"][0]["entries"].append({"text": "bad", "item_ids": [99]})
    before = repr(result)
    enforce_citations(result, URLS.keys())
    assert repr(result) == before


def test_enforce_citations_accepts_any_collection_of_known_ids():
    result = {"buckets": [{"name": "F", "entries": [{"text": "x", "item_ids": [11]}]}]}
    clean, dropped = enforce_citations(result, [11, 12])
    assert dropped == []
    assert clean["buckets"][0]["entries"] == [{"text": "x", "item_ids": [11]}]


def test_enforce_citations_keeps_an_emptied_extra_section_key_but_renders_nothing():
    result = {
        "buckets": [{"name": "F", "entries": [{"text": "ok", "item_ids": [11]}]}],
        "watch_list": [{"text": "bad", "item_ids": []}],
    }
    clean, _ = enforce_citations(result, URLS.keys())
    assert clean["watch_list"] == []
    assert "## Watch list" not in render_markdown(clean, URLS, title="T")


def test_unknown_top_level_keys_are_ignored_and_preserved():
    result = _result()
    result["merged"] = {"14": [11, 12]}
    clean, _ = enforce_citations(result, URLS.keys())
    assert clean["merged"] == {"14": [11, 12]}
    assert "merged" not in render_markdown(clean, URLS, title="T")


def test_missing_buckets_and_missing_extra_section_are_treated_as_empty():
    clean, dropped = enforce_citations({}, URLS.keys())
    assert clean == {}
    assert dropped == []
    out = render_digest({}, URLS, title="T")
    assert out == RenderResult(markdown="", kept=0, dropped=[])


def test_empty_bucket_list_and_empty_entry_list_render_nothing():
    out = render_digest(
        {"buckets": [{"name": "F", "entries": []}], "watch_list": []},
        URLS,
        title="T",
    )
    assert out.markdown == ""
    assert out.dropped == []


# --- render_markdown on its own ---------------------------------------------


def test_render_markdown_trusts_its_input_and_raises_on_an_id_it_cannot_link():
    result = {"buckets": [{"name": "F", "entries": [{"text": "x", "item_ids": [99]}]}]}
    with pytest.raises(KeyError) as excinfo:
        render_markdown(result, URLS, title="T")
    assert 99 in excinfo.value.args


def test_render_markdown_renders_an_uncited_entry_it_was_handed():
    result = {"buckets": [{"name": "F", "entries": [{"text": "x", "item_ids": []}]}]}
    assert render_markdown(result, URLS, title="T") == "# T\n\n## F\n\n- x\n"


# --- entry text and links ---------------------------------------------------


def test_multiline_entry_text_collapses_to_one_bullet():
    result = {
        "buckets": [
            {
                "name": "F",
                "entries": [
                    {"text": "line one\n  line two\r\nline three", "item_ids": [11]}
                ],
            }
        ]
    }
    md = render_digest(result, URLS, title="T").markdown
    assert "- line one line two line three [11](https://example.edu/a)\n" in md
    assert len([line for line in md.splitlines() if line.startswith("- ")]) == 1


def test_duplicate_ids_in_one_entry_link_once_in_first_seen_order():
    result = {
        "buckets": [{"name": "F", "entries": [{"text": "x", "item_ids": [12, 11, 12]}]}]
    }
    md = render_digest(result, URLS, title="T").markdown
    assert "- x [12](https://example.edu/b) [11](https://example.edu/a)\n" in md


def test_link_label_callable_replaces_the_bare_id():
    labels = {11: "award", 12: "paper"}
    md = render_digest(
        _result(), URLS, title="T", link_label=lambda i: labels.get(i, f"#{i}")
    ).markdown
    assert "[award](https://example.edu/a)" in md
    assert "[#13](https://example.edu/c)" in md


def test_unicode_text_and_urls_survive_unchanged():
    urls = {7: "https://example.edu/caf%C3%A9?q=α+β"}
    result = {
        "buckets": [
            {
                "name": "Café — notes",
                "entries": [{"text": "机器学习 — “quoted” ünïcode", "item_ids": [7]}],
            }
        ]
    }
    md = render_digest(result, urls, title="Ünïcode", subtitle="週").markdown
    assert "# Ünïcode\n\n週\n\n## Café — notes\n" in md
    assert (
        "- 机器学习 — “quoted” ünïcode [7](https://example.edu/caf%C3%A9?q=α+β)\n" in md
    )


# --- generalized extra sections ---------------------------------------------


def test_default_extra_sections_is_the_watch_list_mapping():
    assert dict(DEFAULT_EXTRA_SECTIONS) == {"watch_list": "Watch list"}


def test_a_second_app_can_name_its_own_extra_sections():
    result = {
        "buckets": [{"name": "F", "entries": [{"text": "b", "item_ids": [11]}]}],
        "open_questions": [{"text": "q", "item_ids": [12]}],
        "watch_list": [{"text": "ignored", "item_ids": [13]}],
    }
    out = render_digest(
        result,
        URLS,
        title="T",
        extra_sections={"open_questions": "Open questions"},
    )
    assert "## Open questions\n\n- q [12](https://example.edu/b)" in out.markdown
    assert "ignored" not in out.markdown
    assert out.kept == 2


def test_extra_sections_render_in_mapping_order():
    result = {
        "watch_list": [{"text": "w", "item_ids": [11]}],
        "open_questions": [{"text": "q", "item_ids": [11]}],
    }
    md = render_digest(
        result,
        URLS,
        title="T",
        extra_sections={"open_questions": "Open questions", "watch_list": "Watch list"},
    ).markdown
    assert md.index("## Open questions") < md.index("## Watch list")


# --- malformed input --------------------------------------------------------


@pytest.mark.parametrize(
    "result",
    [
        {"buckets": {"name": "F"}},
        {"buckets": "Funding"},
        {"buckets": [{"name": "F", "entries": "x"}]},
        {"buckets": [{"name": "F", "entries": [{"text": "x", "item_ids": 11}]}]},
        {"buckets": [{"name": "F", "entries": [{"text": "x", "item_ids": "11"}]}]},
        {"buckets": [{"name": "F", "entries": ["x"]}]},
        {"buckets": [{"name": 7, "entries": []}]},
        {"buckets": [{"name": "F", "entries": [{"text": 7, "item_ids": [11]}]}]},
        {"watch_list": "w"},
    ],
)
def test_malformed_structure_raises_type_error(result):
    with pytest.raises(TypeError):
        enforce_citations(result, URLS.keys())


@pytest.mark.parametrize(
    "result",
    [
        {"buckets": [{"entries": [{"text": "x", "item_ids": [11]}]}]},
        {"buckets": [{"name": "F", "entries": [{"item_ids": [11]}]}]},
    ],
)
def test_missing_required_key_raises_value_error(result):
    with pytest.raises(ValueError):
        enforce_citations(result, URLS.keys())


def test_result_that_is_not_a_mapping_raises_type_error():
    with pytest.raises(TypeError):
        enforce_citations([{"name": "F"}], URLS.keys())
    with pytest.raises(TypeError):
        render_markdown([], URLS, title="T")


def test_dataclasses_are_frozen():
    drop = DroppedEntry(section="F", text="x", item_ids=[], reason="no_ids")
    with pytest.raises(Exception):
        drop.text = "y"
    out = RenderResult(markdown="", kept=0, dropped=[])
    with pytest.raises(Exception):
        out.kept = 1


# --- added by review --------------------------------------------------------


def test_a_bool_is_never_an_id_even_though_python_says_true_equals_one():
    """``True == 1``, so an unguarded ``in`` check would link ``true`` to item 1."""
    result = {
        "buckets": [{"name": "F", "entries": [{"text": "x", "item_ids": [True]}]}]
    }
    out = render_digest(result, {1: "https://example.edu/one"}, title="T")
    assert out.markdown == ""
    assert out.kept == 0
    assert out.dropped[0].reason == "unknown_id"
    assert out.dropped[0].unknown == (True,)


def test_a_bool_is_unknown_even_when_the_caller_declared_it():
    _, dropped = enforce_citations(
        {"buckets": [{"name": "F", "entries": [{"text": "x", "item_ids": [False]}]}]},
        [False, 11],
    )
    assert dropped[0].reason == "unknown_id"


def test_a_blank_line_inside_entry_text_collapses_to_a_single_space():
    result = {
        "buckets": [
            {"name": "F", "entries": [{"text": "one\n\n  two\r\n\r\nthree", "item_ids": [11]}]}
        ]
    }
    md = render_digest(result, URLS, title="T").markdown
    assert "- one two three [11](https://example.edu/a)\n" in md


def test_internal_spacing_other_than_newlines_is_left_verbatim():
    result = {
        "buckets": [
            {"name": "F", "entries": [{"text": "  a  b\tc  ", "item_ids": [11]}]}
        ]
    }
    md = render_digest(result, URLS, title="T").markdown
    assert "- a  b\tc [11](https://example.edu/a)\n" in md


def test_empty_entry_text_does_not_leave_a_doubled_space_before_the_link():
    result = {"buckets": [{"name": "F", "entries": [{"text": "   ", "item_ids": [11]}]}]}
    md = render_digest(result, URLS, title="T").markdown
    assert "- [11](https://example.edu/a)\n" in md
    assert "-  " not in md


def test_default_extra_sections_cannot_be_mutated_by_a_caller():
    with pytest.raises(TypeError):
        DEFAULT_EXTRA_SECTIONS["open_questions"] = "Open questions"  # type: ignore[index]
    assert dict(DEFAULT_EXTRA_SECTIONS) == {"watch_list": "Watch list"}


def test_render_digest_rejects_a_urls_that_is_not_a_mapping():
    with pytest.raises(TypeError):
        render_digest({}, [11, 12], title="T")  # type: ignore[arg-type]
