"""Tests for profile loading, the overlay order, and fetch deduplication.

The claim these defend is the spec's central one: adding a profile is a YAML
file and never a code change. If any of these need editing to add a profile,
the abstraction has failed.
"""

from __future__ import annotations

import pytest

from brief.lib.config_env_interpolate import MissingConfigVar
from brief.models import Profile
from brief.profile import (
    ProfileError,
    fetch_plan,
    load_all_profiles,
    load_profile,
    load_sources,
    load_yaml,
)

CONFIG = {
    "db": "data/items.db",
    "smtp": {"host": "relay.example.test", "port": 25},
    "llm": {"provider": "claude-cli", "timeout_s": 600},
    "delivery": {"from": "default@example.test", "to": ["default@example.test"]},
    "select": {"chars_per_token": 4, "max_item_chars": 6000},
}

SOURCES = {
    "feed_a": {"kind": "rss", "url": "https://example.test/feed"},
    "nsf": {"kind": "nsf"},
    "nih": {"kind": "nih"},
}

MINIMAL = """
name: demo
title: Demo Brief
audience: a reader
persona: an analyst
sources:
  - feed_a
relevance:
  any_of: [thing]
buckets:
  - {name: Funding, ask: awards}
delivery:
  from: demo@example.test
  to: [reader@example.test]
"""


def write(tmp_path, text, name="demo.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def load(tmp_path, text, *, config=None, sources=None, env=None, name="demo.yaml"):
    return load_profile(
        write(tmp_path, text, name),
        config=config if config is not None else CONFIG,
        sources=sources if sources is not None else SOURCES,
        env=env or {},
    )


# --- the happy path -------------------------------------------------------


def test_a_minimal_profile_loads_with_config_defaults(tmp_path):
    profile = load(tmp_path, MINIMAL)
    assert isinstance(profile, Profile)
    assert profile.name == "demo"
    assert profile.cadence == "weekly"
    assert profile.llm["provider"] == "claude-cli", "inherited from config.yaml"
    assert profile.delivery.to == ("reader@example.test",), "profile wins over config"


def test_a_profile_overrides_only_what_it_names(tmp_path):
    profile = load(tmp_path, MINIMAL + "\nllm:\n  provider: local\n")
    assert profile.llm["provider"] == "local"
    assert profile.llm["timeout_s"] == 600, "the unnamed key survives the overlay"


def test_a_nested_override_does_not_drop_sibling_keys(tmp_path):
    profile = load(tmp_path, MINIMAL + "\nselect:\n  max_item_chars: 1000\n")
    assert profile.config["select"]["max_item_chars"] == 1000
    assert profile.config["select"]["chars_per_token"] == 4, (
        "the deep merge must descend"
    )


def test_source_overrides_are_kept_per_profile(tmp_path):
    text = MINIMAL.replace(
        "  - feed_a", '  - feed_a\n  - nsf: {awardee: "Some University"}'
    )
    profile = load(tmp_path, text)
    assert [r.name for r in profile.sources] == ["feed_a", "nsf"]
    assert profile.sources[1].params == {"awardee": "Some University"}


def test_a_source_with_no_overrides_carries_empty_params(tmp_path):
    profile = load(tmp_path, MINIMAL)
    assert profile.sources[0].params == {}


# --- interpolation --------------------------------------------------------


def test_a_variable_in_sources_is_resolved(tmp_path):
    path = write(
        tmp_path, 'feed:\n  kind: rss\n  url: "http://h/rss?token=${TOK}"\n', "s.yaml"
    )
    sources = load_sources(path, {"TOK": "secret-value"})
    assert sources["feed"]["url"] == "http://h/rss?token=secret-value"


def test_a_missing_variable_names_the_variable_and_the_key_path(tmp_path):
    path = write(
        tmp_path, 'feed:\n  kind: rss\n  url: "http://h/rss?token=${TOK}"\n', "s.yaml"
    )
    with pytest.raises(MissingConfigVar) as caught:
        load_sources(path, {})
    assert caught.value.variable == "TOK"
    assert "url" in caught.value.path


def test_a_variable_default_is_used_when_unset(tmp_path):
    path = write(
        tmp_path, 'feed:\n  kind: rss\n  url: "http://h:${PORT:-8080}/rss"\n', "s.yaml"
    )
    assert load_sources(path, {})["feed"]["url"] == "http://h:8080/rss"


# --- validation -----------------------------------------------------------


@pytest.mark.parametrize(
    "removed,field",
    [
        ("name: demo", "name"),
        ("title: Demo Brief", "title"),
        ("audience: a reader", "audience"),
        ("persona: an analyst", "persona"),
    ],
)
def test_a_missing_required_field_is_named(tmp_path, removed, field):
    with pytest.raises(ProfileError) as caught:
        load(tmp_path, MINIMAL.replace(removed, ""))
    assert caught.value.field == field


def test_an_unknown_source_lists_what_is_available(tmp_path):
    with pytest.raises(ProfileError) as caught:
        load(tmp_path, MINIMAL.replace("  - feed_a", "  - typo_source"))
    assert "typo_source" in str(caught.value)
    assert "feed_a" in str(caught.value), "the message must name the real options"


def test_an_empty_any_of_is_refused(tmp_path):
    with pytest.raises(ProfileError) as caught:
        load(tmp_path, MINIMAL.replace("  any_of: [thing]", "  any_of: []"))
    assert "any_of" in caught.value.field
    assert "every item" in caught.value.problem, "the reason must say why it matters"


def test_no_sources_is_refused(tmp_path):
    with pytest.raises(ProfileError) as caught:
        load(tmp_path, MINIMAL.replace("sources:\n  - feed_a\n", "sources: []\n"))
    assert caught.value.field == "sources"


def test_no_buckets_is_refused(tmp_path):
    with pytest.raises(ProfileError) as caught:
        load(tmp_path, MINIMAL.replace("  - {name: Funding, ask: awards}", ""))
    assert caught.value.field == "buckets"


def test_duplicate_bucket_names_are_refused(tmp_path):
    text = MINIMAL.replace(
        "  - {name: Funding, ask: awards}",
        "  - {name: Funding, ask: awards}\n  - {name: Funding, ask: again}",
    )
    with pytest.raises(ProfileError) as caught:
        load(tmp_path, text)
    assert "duplicate" in caught.value.problem


def test_no_recipients_is_refused(tmp_path):
    text = MINIMAL.replace("  to: [reader@example.test]", "  to: []")
    with pytest.raises(ProfileError) as caught:
        load(tmp_path, text, config={**CONFIG, "delivery": {"from": "x@example.test"}})
    assert "to" in caught.value.field


def test_a_single_recipient_string_is_accepted_as_a_list(tmp_path):
    profile = load(
        tmp_path,
        MINIMAL.replace("  to: [reader@example.test]", "  to: reader@example.test"),
    )
    assert profile.delivery.to == ("reader@example.test",)


def test_broken_yaml_names_the_file(tmp_path):
    with pytest.raises(ProfileError) as caught:
        load(tmp_path, "name: demo\n  bad indent: [\n")
    assert caught.value.path.endswith("demo.yaml")


def test_a_yaml_file_that_is_a_list_is_refused(tmp_path):
    with pytest.raises(ProfileError) as caught:
        load_yaml(write(tmp_path, "- one\n- two\n"))
    assert caught.value.field == "<root>"


def test_an_empty_yaml_file_loads_as_an_empty_mapping(tmp_path):
    assert load_yaml(write(tmp_path, "")) == {}


def test_a_source_without_a_kind_is_refused(tmp_path):
    path = write(tmp_path, "feed:\n  url: https://example.test/f\n", "s.yaml")
    with pytest.raises(ProfileError) as caught:
        load_sources(path, {})
    assert "kind" in caught.value.problem


# --- fetch planning -------------------------------------------------------


def test_two_profiles_sharing_a_source_produce_one_fetch(tmp_path):
    a = load(tmp_path, MINIMAL, name="a.yaml")
    b = load(tmp_path, MINIMAL.replace("name: demo", "name: other"), name="b.yaml")
    assert fetch_plan([a, b]) == [("feed_a", {}, "feed_a")]


def test_the_same_source_with_different_params_is_two_fetches(tmp_path):
    a = load(
        tmp_path,
        MINIMAL.replace("  - feed_a", '  - nsf: {awardee: "One"}'),
        name="a.yaml",
    )
    b = load(
        tmp_path,
        MINIMAL.replace("name: demo", "name: other").replace(
            "  - feed_a", '  - nsf: {awardee: "Two"}'
        ),
        name="b.yaml",
    )
    plan = fetch_plan([a, b])
    assert len(plan) == 2
    assert {p[1]["awardee"] for p in plan} == {"One", "Two"}
    assert len({p[2] for p in plan}) == 2, (
        "two parameter sets must store under two keys, or each profile's window "
        "picks up the other's awards"
    )


def test_the_fetch_plan_is_ordered_so_a_run_is_reproducible(tmp_path):
    a = load(
        tmp_path, MINIMAL.replace("  - feed_a", "  - nsf\n  - feed_a"), name="a.yaml"
    )
    assert fetch_plan([a]) == fetch_plan([a])


# --- the whole directory --------------------------------------------------


def test_loading_a_directory_finds_every_profile(tmp_path):
    directory = tmp_path / "profiles"
    directory.mkdir()
    for name in ("one", "two"):
        (directory / f"{name}.yaml").write_text(
            MINIMAL.replace("name: demo", f"name: {name}"), encoding="utf-8"
        )
    profiles = load_all_profiles(directory, config=CONFIG, sources=SOURCES, env={})
    assert [p.name for p in profiles] == ["one", "two"]


def test_adding_a_profile_needs_no_code(tmp_path):
    """The abstraction's acceptance test: a new topic is a new file.

    A profile with a different topic, audience, sources, and section names
    loads through exactly the same path as the first one.
    """
    directory = tmp_path / "profiles"
    directory.mkdir()
    (directory / "faculty.yaml").write_text(
        """
name: maize
title: Maize genomics weekly
audience: one PI and their lab
persona: a research assistant
sources:
  - nih: {advanced_text_search: "maize genome"}
relevance:
  any_of: [maize, Zea mays]
  none_of: [sweet corn recipe]
  max_items: 60
buckets:
  - {name: Papers and preprints, ask: new results}
  - {name: Funding opportunities, ask: open calls with deadlines}
delivery:
  from: brief@example.test
  to: [pi@example.test]
llm:
  provider: local
""",
        encoding="utf-8",
    )
    (profile,) = load_all_profiles(directory, config=CONFIG, sources=SOURCES, env={})
    assert profile.llm["provider"] == "local"
    assert [b.name for b in profile.buckets] == [
        "Papers and preprints",
        "Funding opportunities",
    ]
    assert profile.relevance.none_of == ("sweet corn recipe",)
    assert profile.sources[0].params == {"advanced_text_search": "maize genome"}


def test_every_shipped_profile_loads(tmp_path):
    """The real profiles/, against the real sources.yaml.

    Asserts that each one loads rather than naming them: this used to pin the
    exact list, so adding a profile — the thing the design is FOR — failed a
    test about something else."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    sources = load_sources(root / "sources.yaml", {"CD_TOKEN": "x"})
    config = load_yaml(root / "config.yaml")

    on_disk = sorted(p.stem for p in (root / "profiles").glob("*.yaml"))
    profiles = load_all_profiles(
        root / "profiles", config=config, sources=sources, env={"CD_TOKEN": "x"}
    )

    assert sorted(p.name for p in profiles) == on_disk, "every file loaded"
    for profile in profiles:
        assert profile.buckets, f"{profile.name} declares no sections"
        assert profile.sources, f"{profile.name} declares no sources"


# --- found by the adversarial review; each of these was a real defect --------


def test_a_profile_with_no_subject_gets_the_real_default_not_a_descriptor(tmp_path):
    """`slots=True` makes `Delivery.subject` a member descriptor, not a string.

    Reading it as a default emailed `<member 'subject' of 'Delivery' objects>`
    as the subject line of a real brief.
    """
    from brief.models import DEFAULT_SUBJECT

    profile = load(tmp_path, MINIMAL)
    assert profile.delivery.subject == DEFAULT_SUBJECT
    assert "member" not in profile.delivery.subject
    assert "{title}" in profile.delivery.subject


@pytest.mark.parametrize("field", ["any_of", "none_of"])
def test_a_keyword_list_written_as_a_bare_string_is_refused(tmp_path, field):
    """`any_of: genome` is natural YAML and iterates to one keyword per character."""
    text = (
        MINIMAL.replace("  any_of: [thing]", "  any_of: genome")
        if field == "any_of"
        else MINIMAL.replace("  any_of: [thing]", "  any_of: [thing]\n  none_of: genome")
    )
    with pytest.raises(ProfileError) as caught:
        load(tmp_path, text)
    assert field in caught.value.field
    assert "per character" in caught.value.problem, "the message must explain the trap"


def test_a_blank_keyword_is_refused(tmp_path):
    """An empty pattern matches every item, turning the filter off silently."""
    with pytest.raises(ProfileError) as caught:
        load(tmp_path, MINIMAL.replace('  any_of: [thing]', '  any_of: [thing, ""]'))
    assert "blank" in caught.value.problem


def test_two_profiles_with_the_same_name_are_refused(tmp_path):
    """Both would run and the second would overwrite the first's stored brief."""
    directory = tmp_path / "profiles"
    directory.mkdir()
    for filename in ("a.yaml", "b.yaml"):
        (directory / filename).write_text(MINIMAL, encoding="utf-8")
    with pytest.raises(ProfileError) as caught:
        load_all_profiles(directory, config=CONFIG, sources=SOURCES, env={})
    assert "duplicates" in caught.value.problem
    assert "a.yaml" in caught.value.problem, "the message must name the other file"


def test_a_profile_saved_as_yml_is_not_silently_ignored(tmp_path):
    """Before this, a `.yml` profile produced no brief, no error, and no log line."""
    directory = tmp_path / "profiles"
    directory.mkdir()
    (directory / "demo.yml").write_text(MINIMAL, encoding="utf-8")
    profiles = load_all_profiles(directory, config=CONFIG, sources=SOURCES, env={})
    assert [p.name for p in profiles] == ["demo"]


def test_each_distinct_parameter_set_gets_its_own_storage_key(tmp_path):
    """The contamination fix, at the planning layer."""
    a = load(tmp_path, MINIMAL.replace("  - feed_a", '  - nsf: {awardee: "One"}'), name="a.yaml")
    b = load(
        tmp_path,
        MINIMAL.replace("name: demo", "name: other").replace(
            "  - feed_a", '  - nsf: {keyword: "two"}'
        ),
        name="b.yaml",
    )
    keys = {key for _n, _p, key in fetch_plan([a, b])}
    assert len(keys) == 2
    assert all(k.startswith("nsf#") for k in keys), "the name stays readable in the key"


def test_relevance_tags_are_parsed_and_default_empty(tmp_path):
    prof = load(tmp_path, MINIMAL.replace("  any_of: [thing]\n", "  any_of: [thing]\n  tags: [genomics, crispr]\n"))
    assert prof.relevance.tags == ("genomics", "crispr")
    assert load(tmp_path, MINIMAL).relevance.tags == ()


def test_relevance_tags_refuse_a_bare_string(tmp_path):
    with pytest.raises(ProfileError, match="relevance.tags"):
        load(tmp_path, MINIMAL.replace("  any_of: [thing]\n", "  any_of: [thing]\n  tags: genomics\n"))
