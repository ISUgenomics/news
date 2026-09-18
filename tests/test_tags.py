"""Tags: the vocabulary file, derivation from labels and phrases, and storage.

Stub, never mock: a real YAML file on disk, a real SQLite database, the real
vendored resolver.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from brief import db, tags
from brief.models import Item

NOW = datetime(2026, 9, 17, 6, 0, tzinfo=timezone.utc)

VOCAB = """
registry:
  genomics:
    match: [genomics, genome, genomic]
  crispr:
    match: [CRISPR, "gene editing"]
  rust: {}
aliases:
  genome-analysis: genomics
  ml: machine-learning-not-a-term
"""


def _vocab(tmp_path, text=VOCAB.replace("ml: machine-learning-not-a-term\n", "")):
    path = tmp_path / "tags.yaml"
    path.write_text(text)
    return tags.load_vocabulary(path)


# --- loading ---------------------------------------------------------------


def test_missing_file_is_an_empty_vocabulary_not_an_error(tmp_path):
    vocab = tags.load_vocabulary(tmp_path / "absent.yaml")
    assert not vocab
    assert tags.derive_tags(["Genomics"], "genome", vocab) == ([], [])


def test_registry_terms_aliases_and_matchers_load(tmp_path):
    vocab = _vocab(tmp_path)
    assert set(vocab.registry) == {"genomics", "crispr", "rust"}
    assert vocab.aliases == {"genome-analysis": "genomics"}
    assert set(vocab.matchers) == {"genomics", "crispr"}  # rust declares no phrases


@pytest.mark.parametrize(
    "text,needle",
    [
        ("registry:\n  Genomics: {}\n", "not kebab-case"),
        ("registry:\n  genomics:\n    match: genomics\n", "list of non-blank phrases"),
        ("registry:\n  genomics: {}\naliases:\n  ml: machine-learning\n", "not a registry term"),
        ("registry:\n  genomics: {}\naliases:\n  genomics: genomics\n", "both a registry term and an alias"),
        ("- not\n- a mapping\n", "must be a mapping"),
    ],
)
def test_a_malformed_vocabulary_names_the_problem(tmp_path, text, needle):
    (tmp_path / "tags.yaml").write_text(text)
    with pytest.raises(tags.TagsError, match=needle):
        tags.load_vocabulary(tmp_path / "tags.yaml")


# --- derivation ------------------------------------------------------------


def test_source_labels_resolve_to_registry_terms_with_origin_source(tmp_path):
    vocab = _vocab(tmp_path)
    pairs, new = tags.derive_tags(["Genomic", "genome analysis", "Rust"], "", vocab)
    assert pairs == [("genomics", "source"), ("rust", "source")]
    assert new == []


def test_phrases_tag_text_that_carries_no_labels(tmp_path):
    vocab = _vocab(tmp_path)
    pairs, new = tags.derive_tags([], "A CRISPR screen of the maize genome", vocab)
    assert pairs == [("genomics", "phrase"), ("crispr", "phrase")]
    assert new == []


def test_a_label_wins_the_origin_over_a_phrase_and_a_tag_is_stored_once(tmp_path):
    vocab = _vocab(tmp_path)
    pairs, _ = tags.derive_tags(["Genomics"], "the genome of maize", vocab)
    assert pairs == [("genomics", "source")]


def test_unplaced_labels_are_reported_once_and_never_returned_as_tags(tmp_path):
    vocab = _vocab(tmp_path)
    pairs, new = tags.derive_tags(["Phylogenetics", "phylogenetics", "Genomics"], "", vocab)
    assert pairs == [("genomics", "source")]
    assert new == ["phylogenetics"]


def test_short_junk_labels_are_dropped_before_resolution(tmp_path):
    vocab = _vocab(tmp_path)
    pairs, new = tags.derive_tags(["a", "C++", "!!!", ""], "", vocab)
    assert pairs == [] and new == []


def test_unknown_tags_is_the_profile_check(tmp_path):
    vocab = _vocab(tmp_path)
    assert tags.unknown_tags(["genomics", "genome-analysis", "plasma"], vocab) == ["plasma"]


# --- storage ---------------------------------------------------------------


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "items.db")
    db.upsert_items(
        c,
        [
            Item(source="feed_a", url="https://example.test/1", title="Maize genome", body="a genome"),
            Item(source="feed_a", url="https://example.test/2", title="Parking", body="lot 4"),
        ],
        now=NOW,
    )
    yield c
    c.close()


def test_item_tags_table_exists_on_a_fresh_database(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "item_tags" in names


def test_item_tags_table_is_added_to_a_database_created_before_it(tmp_path):
    """The stamp never migrates; the DDL's IF NOT EXISTS does the work."""
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(db.DDL.split("CREATE TABLE IF NOT EXISTS item_tags")[0])
    old.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    old.execute("INSERT INTO schema_version VALUES (3)")
    old.commit()
    old.close()
    c = db.connect(path)
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "item_tags" in names
    assert c.execute("SELECT version FROM schema_version").fetchone()[0] == 3  # unchanged, by design


def test_replace_tags_round_trips_and_is_idempotent(conn):
    item_id = conn.execute("SELECT id FROM items WHERE title='Maize genome'").fetchone()[0]
    assert db.replace_tags(conn, item_id, [("genomics", "phrase"), ("rust", "source")]) is True
    assert db.replace_tags(conn, item_id, [("rust", "source"), ("genomics", "phrase")]) is False
    assert db.tags_for(conn, [item_id]) == {item_id: ["genomics", "rust"]}
    assert db.replace_tags(conn, item_id, [("genomics", "phrase")]) is True
    assert db.tags_for(conn, [item_id]) == {item_id: ["genomics"]}
    assert db.replace_tags(conn, item_id, []) is True
    assert db.tags_for(conn, [item_id]) == {}


def test_tagging_changes_neither_row_count_nor_content_hash(conn):
    before = conn.execute("SELECT id, content_hash FROM items ORDER BY id").fetchall()
    for item_id, *_ in before:
        db.replace_tags(conn, item_id, [("genomics", "phrase")])
    after = conn.execute("SELECT id, content_hash FROM items ORDER BY id").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]


def test_tag_counts_by_tag_and_by_source(conn):
    ids = [r[0] for r in conn.execute("SELECT id FROM items ORDER BY id")]
    db.replace_tags(conn, ids[0], [("genomics", "phrase"), ("rust", "source")])
    db.replace_tags(conn, ids[1], [("genomics", "phrase")])
    assert db.tag_counts(conn) == [("genomics", 2), ("rust", 1)]
    assert db.tag_counts(conn, by_source=True) == [("genomics", "feed_a", 2), ("rust", "feed_a", 1)]


def test_iter_taggable_yields_text_and_parsed_raw(conn):
    rows = list(db.iter_taggable(conn))
    assert [(r[1], r[2], r[4]) for r in rows] == [("feed_a", "Maize genome", None), ("feed_a", "Parking", None)]
    assert [r[3] for r in rows] == ["a genome", "lot 4"]
