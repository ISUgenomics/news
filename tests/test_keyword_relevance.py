"""Behaviour tests for the keyword_relevance seed.

One test per documented contract line plus the edge cases named in
docs/seeds/keyword-relevance.md. Pure stdlib: no network, no stubs needed.
"""

import re

from brief.lib.keyword_relevance import compile_terms, filter_ranked, score_text


# --- fixtures -------------------------------------------------------------


def rec(text):
    """A record is opaque to the module; a tuple is enough to prove that."""
    return ("id", text)


def text_of(r):
    return r[1]


# --- compile_terms --------------------------------------------------------


def test_compile_terms_returns_one_pattern_per_term():
    pats = compile_terms(["alpha", "beta"])
    assert len(pats) == 2
    assert all(isinstance(p, re.Pattern) for p in pats)


def test_compile_terms_deduplicates_case_insensitively_preserving_order():
    pats = compile_terms(["Alpha", "alpha", "ALPHA", "beta"])
    assert len(pats) == 2
    assert pats[0].search("an alpha record")
    assert pats[1].search("a beta record")


def test_compile_terms_ignores_blank_terms():
    assert compile_terms(["", "   ", "\t\n", "alpha"]) != []
    assert len(compile_terms(["", "   ", "alpha"])) == 1
    assert compile_terms(["", "  "]) == []


def test_compile_terms_patterns_are_case_insensitive():
    (pat,) = compile_terms(["Machine Learning"])
    assert pat.search("MACHINE LEARNING is here")


def test_compile_terms_treats_terms_as_literals_not_regex():
    (pat,) = compile_terms(["a.b"], whole_word=False)
    assert pat.search("a.b")
    assert not pat.search("axb")


def test_compile_terms_phrase_matches_across_a_line_break():
    (pat,) = compile_terms(["machine learning"])
    assert pat.search("about machine\n   learning today")


def test_compile_terms_whole_word_default_does_not_match_inside_a_word():
    (pat,) = compile_terms(["AI"])
    assert not pat.search("he said nothing")
    assert pat.search("AI research")


def test_compile_terms_whole_word_allows_terms_ending_in_non_word_chars():
    """The \\w-lookaround rule: \\b would refuse these outright."""
    for term, hay in [("C++", "the C++ toolchain"), ("R&D", "our R&D budget"), (".NET", "on .NET 8")]:
        (pat,) = compile_terms([term])
        assert pat.search(hay), f"{term!r} should hit {hay!r}"


def test_compile_terms_whole_word_still_guards_the_word_side_of_such_terms():
    (pat,) = compile_terms(["C++"])
    assert not pat.search("C++11 only")


def test_compile_terms_whole_word_false_is_plain_substring():
    (pat,) = compile_terms(["AI"], whole_word=False)
    assert pat.search("he said nothing")


def test_compile_terms_matches_unicode_case_insensitively():
    (pat,) = compile_terms(["café"])
    assert pat.search("a CAFÉ in Paris")
    assert not pat.search("cafés")  # whole-word: é..s is still one word


def test_compile_terms_handles_non_latin_scripts():
    (pat,) = compile_terms(["Ελλάδα"])
    assert pat.search("νέα από την Ελλάδα σήμερα")


def test_compile_terms_dedup_collapses_whitespace_and_strips_edges():
    """Documented: the dedup key is the whitespace-collapsed term, edges stripped."""
    pats = compile_terms(["  Machine Learning ", "machine\n  learning", "MACHINE\tLEARNING"])
    assert len(pats) == 1
    assert pats[0].search("a machine learning paper")


def test_compile_terms_dedup_keeps_terms_that_ignorecase_keeps_apart():
    """Regression: a casefold key merged these two, silently losing the second term.

    "Stra\u00dfe".casefold() == "STRASSE".casefold(), but re.IGNORECASE does not treat
    \u00df and "ss" as equal -- so deduping on casefold left one pattern that could never
    match "STRASSE", and any record carrying only that spelling quietly failed to survive.
    """
    assert not re.compile("Stra\u00dfe", re.IGNORECASE).search("STRASSE")
    pats = compile_terms(["Stra\u00dfe", "STRASSE"])
    assert len(pats) == 2
    assert any(p.search("STRASSE heute") for p in pats)
    assert any(p.search("in der Stra\u00dfe") for p in pats)


def test_filter_ranked_keeps_a_record_matching_only_the_full_fold_variant():
    """End-to-end shape of the same regression: no silently-dropped keep term."""
    r = rec("die STRASSE ist gesperrt")
    assert filter_ranked([r], text_of, ["Stra\u00dfe", "STRASSE"]) == [(r, 1)]


def test_filter_ranked_whitespace_variants_of_a_term_score_one_hit():
    r = rec("a machine learning story")
    assert filter_ranked([r], text_of, ["machine learning", "Machine  Learning"]) == [(r, 1)]


# --- score_text -----------------------------------------------------------


def test_score_text_counts_distinct_terms_not_occurrences():
    any_of = compile_terms(["machine learning"])
    assert score_text("machine learning " * 9, any_of) == 1


def test_score_text_counts_each_distinct_term_once():
    any_of = compile_terms(["alpha", "beta", "gamma"])
    assert score_text("alpha beta alpha beta", any_of) == 2


def test_score_text_returns_none_when_no_any_of_term_hits():
    assert score_text("nothing relevant here", compile_terms(["alpha"])) is None


def test_score_text_returns_none_when_a_none_of_term_hits():
    any_of = compile_terms(["alpha"])
    none_of = compile_terms(["sponsored"])
    assert score_text("alpha alpha, sponsored post", any_of, none_of) is None


def test_score_text_checks_none_of_first_and_absolutely():
    """Every any_of term hitting does not rescue a record; exclusion wins."""
    any_of = compile_terms(["alpha", "beta"])
    none_of = compile_terms(["press release"])
    assert score_text("alpha and beta, a press release", any_of, none_of) is None


def test_score_text_empty_any_of_scores_zero():
    assert score_text("anything at all", []) == 0


def test_score_text_empty_any_of_still_honours_none_of():
    assert score_text("a sponsored thing", [], compile_terms(["sponsored"])) is None


def test_score_text_treats_none_as_empty_text():
    assert score_text(None, []) == 0
    assert score_text(None, compile_terms(["alpha"])) is None


def test_score_text_empty_string_behaves_like_none():
    assert score_text("", compile_terms(["alpha"])) is None


def test_score_text_reuses_patterns_across_calls():
    any_of = compile_terms(["alpha"])
    assert score_text("alpha", any_of) == 1
    assert score_text("alpha alpha", any_of) == 1
    assert score_text("beta", any_of) is None


# --- filter_ranked --------------------------------------------------------


def test_filter_ranked_returns_pairs_of_record_and_hits():
    out = filter_ranked([rec("alpha")], text_of, ["alpha"])
    assert out == [(("id", "alpha"), 1)]


def test_filter_ranked_sorts_by_hits_descending():
    one = rec("alpha only")
    three = rec("alpha beta gamma")
    two = rec("beta gamma")
    out = filter_ranked([one, three, two], text_of, ["alpha", "beta", "gamma"])
    assert [hits for _, hits in out] == [3, 2, 1]
    assert [r for r, _ in out] == [three, two, one]


def test_filter_ranked_sort_is_stable_within_equal_counts():
    first = rec("alpha, newest")
    second = rec("alpha, older")
    third = rec("alpha, oldest")
    out = filter_ranked([first, second, third], text_of, ["alpha"])
    assert [r for r, _ in out] == [first, second, third]


def test_filter_ranked_drops_records_with_no_any_of_hit():
    keep = rec("alpha")
    drop = rec("nothing here")
    out = filter_ranked([keep, drop], text_of, ["alpha"])
    assert out == [(keep, 1)]


def test_filter_ranked_drops_none_of_hits_before_any_of():
    out = filter_ranked([rec("alpha, sponsored")], text_of, ["alpha"], ["sponsored"])
    assert out == []


def test_filter_ranked_empty_any_of_keeps_everything_at_zero_hits():
    a, b = rec("alpha"), rec("wholly unrelated")
    out = filter_ranked([a, b], text_of, [])
    assert out == [(a, 0), (b, 0)]


def test_filter_ranked_empty_any_of_still_excludes_none_of():
    a, b = rec("alpha"), rec("a sponsored post")
    out = filter_ranked([a, b], text_of, [], ["sponsored"])
    assert out == [(a, 0)]


def test_filter_ranked_treats_none_text_as_empty():
    out = filter_ranked([("id", None)], text_of, ["alpha"])
    assert out == []
    out = filter_ranked([("id", None)], text_of, [])
    assert out == [(("id", None), 0)]


def test_filter_ranked_on_empty_records_returns_empty_list():
    assert filter_ranked([], text_of, ["alpha"]) == []


def test_filter_ranked_with_no_survivors_returns_empty_list():
    assert filter_ranked([rec("x"), rec("y")], text_of, ["alpha"]) == []


def test_filter_ranked_accepts_any_iterable_of_opaque_records():
    class Row:
        def __init__(self, body):
            self.body = body

    rows = (Row(b) for b in ["alpha beta", "beta", "nope"])
    out = filter_ranked(rows, lambda r: r.body, ["alpha", "beta"])
    assert [hits for _, hits in out] == [2, 1]
    assert all(isinstance(r, Row) for r, _ in out)


def test_filter_ranked_does_not_require_records_to_be_comparable():
    class Incomparable:
        def __lt__(self, other):  # pragma: no cover - must never be called
            raise AssertionError("records must not be compared")

    a, b = Incomparable(), Incomparable()
    out = filter_ranked([a, b], lambda r: "alpha", ["alpha"])
    assert [r for r, _ in out] == [a, b]


def test_filter_ranked_dedups_terms_so_hits_are_per_distinct_term():
    out = filter_ranked([rec("alpha")], text_of, ["alpha", "ALPHA", "Alpha"])
    assert out == [(("id", "alpha"), 1)]


def test_filter_ranked_ignores_blank_terms():
    out = filter_ranked([rec("alpha")], text_of, ["", "  ", "alpha"])
    assert out == [(("id", "alpha"), 1)]


def test_filter_ranked_all_blank_any_of_is_an_empty_any_of():
    a = rec("wholly unrelated")
    assert filter_ranked([a], text_of, ["", "   "]) == [(a, 0)]


def test_filter_ranked_whole_word_false_matches_substrings():
    out = filter_ranked([rec("he said nothing")], text_of, ["AI"], whole_word=False)
    assert out == [(("id", "he said nothing"), 1)]


def test_filter_ranked_whole_word_true_is_the_default():
    assert filter_ranked([rec("he said nothing")], text_of, ["AI"]) == []


def test_filter_ranked_matches_phrases_across_whitespace_runs():
    r = rec("a story about machine\n\t learning methods")
    assert filter_ranked([r], text_of, ["machine learning"]) == [(r, 1)]


def test_filter_ranked_does_not_consume_the_records_iterable_twice():
    records = iter([rec("alpha"), rec("alpha")])
    assert len(filter_ranked(records, text_of, ["alpha"])) == 2


def test_filter_ranked_calls_text_of_once_per_record():
    calls = []

    def counting(r):
        calls.append(r)
        return r[1]

    filter_ranked([rec("alpha"), rec("beta")], counting, ["alpha"])
    assert len(calls) == 2


def test_module_imports_nothing_from_brief_app_code():
    import brief.lib.keyword_relevance as mod

    src = open(mod.__file__, encoding="utf-8").read()
    assert "from brief" not in src and "import brief" not in src
