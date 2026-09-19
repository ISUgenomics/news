"""distinctive_terms — pure functions over strings; nothing to stub."""

from __future__ import annotations

from brief.lib.distinctive_terms import DEFAULT_STOPWORDS, distinctive_terms, tokenize

FG = [
    "Thermal management of electric batteries for wind energy storage",
    "Electric vehicle batteries: thermal runaway and safety",
    "Wind energy forecasting with thermal sensors",
    "Batteries batteries batteries — a thermal study of energy cells",
]
BG = [
    "A pangenome graph of maize genomes",
    "Transformer models for variant calling in sequencing data",
    "Thermal tolerance genes in wheat",
]


def test_tokenize_is_lowercase_distinct_and_filtered():
    assert tokenize("Batteries, batteries; the THERMAL-runaway of a bat") == {"batteries", "thermal-runaway"}
    assert tokenize("") == set() and tokenize(None) == set()
    assert "purpose" in DEFAULT_STOPWORDS and "purpose" not in tokenize("The purpose of this")
    assert tokenize("abc abcd", min_len=3) == {"abc", "abcd"}


def test_golden_ranking_on_two_small_corpora():
    got = distinctive_terms(FG, BG, min_count=2, min_ratio=1.5)
    assert [(t["term"], t["foreground"], t["background"]) for t in got] == [
        ("thermal", 4, 1),
        ("batteries", 3, 0),
        ("energy", 3, 0),
        ("electric", 2, 0),
        ("wind", 2, 0),
    ]
    # thermal: 4/4 foreground vs 1/3 background -> ratio 3.0
    assert next(t for t in got if t["term"] == "thermal")["ratio"] == 3.0


def test_document_frequency_counts_a_repeated_word_once():
    got = {t["term"]: t["foreground"] for t in distinctive_terms(FG, BG, min_count=1, min_ratio=0)}
    assert got["batteries"] == 3  # one text says it three times


def test_min_count_and_min_ratio_are_floors():
    assert [t["term"] for t in distinctive_terms(FG, BG, min_count=4, min_ratio=0)] == ["thermal"]
    # "thermal" is in 1 of 3 background texts; a ratio floor above 3.0 removes it
    assert "thermal" not in {t["term"] for t in distinctive_terms(FG, BG, min_count=1, min_ratio=3.5)}


def test_an_absent_background_word_is_finite_not_infinite():
    got = {t["term"]: t["ratio"] for t in distinctive_terms(FG, BG, min_count=2, min_ratio=0)}
    assert got["batteries"] == round((3 / 4) / (1 / 3), 2)


def test_empty_foreground_is_empty_and_empty_background_is_allowed():
    assert distinctive_terms([], BG) == []
    got = distinctive_terms(FG, [], min_count=2, min_ratio=0)
    assert got and all(t["background"] == 0 for t in got)


def test_top_caps_the_list_after_sorting():
    assert len(distinctive_terms(FG, BG, min_count=1, min_ratio=0, top=2)) == 2
