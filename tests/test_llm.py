"""Tests for provider selection and the doctor report.

Driven against real stub executables on PATH, so argv construction and the
missing-binary case are exercised through actual subprocess calls rather than
asserted against a mock of them.

The rule these defend: a provider that cannot be used must surface as a state
the operator can read, never as a silent fallback to a different model. A brief
that quietly switched from Claude to an 8B local model would change character
without telling its reader.
"""

from __future__ import annotations

import pytest

from brief import llm
from harness.stub_cli import stub_clis  # noqa: F401  (pytest fixture)


def test_the_protocol_names_only_what_synthesis_needs():
    assert set(llm.LLMProvider.__protocol_attrs__) == {
        "name",
        "available",
        "status",
        "context_window",
        "complete",
        "chat_stream",
        "embed",
    }


def test_every_declared_kind_is_constructible_or_says_why_not():
    for kind in llm.KINDS:
        state = llm.describe({"provider": kind})
        assert state["status"], f"{kind} must always explain itself"
        assert isinstance(state["available"], bool)


def test_a_cli_provider_is_available_when_its_binary_is_on_path(stub_clis):
    stub_clis.install("claude")
    provider = llm.get_provider({"provider": "claude-cli"})
    assert provider.available() is True
    assert provider.context_window() > 0


def test_a_cli_provider_is_unavailable_when_its_binary_is_missing(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("PATH", str(tmp_path))
    provider = llm.get_provider({"provider": "claude-cli"})
    assert provider.available() is False
    assert "claude" in provider.status().lower()


def test_require_provider_refuses_an_unusable_one_with_the_fix_in_the_message(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(llm.ProviderNotAvailable) as caught:
        llm.require_provider({"provider": "claude-cli"})
    assert caught.value.status == caught.value.status.strip()
    assert "claude" in str(caught.value).lower(), (
        "the operator must learn what to install"
    )


def test_a_typo_in_a_profiles_provider_raises_rather_than_defaulting():
    """Defaulting would produce a brief from an unexpected model, silently."""
    with pytest.raises(ValueError) as caught:
        llm.get_provider({"provider": "claude-cli-typo"})
    assert "claude-cli-typo" in str(caught.value)
    for kind in llm.KINDS:
        assert kind in str(caught.value), "the message must list the real options"


def test_the_unimplemented_api_provider_says_so_instead_of_falling_back():
    with pytest.raises(ValueError) as caught:
        llm.get_provider({"provider": "anthropic-api"})
    message = str(caught.value)
    assert "not implemented" in message
    assert "claude-cli" in message, "it must point at something that does work"


def test_an_empty_llm_block_uses_the_documented_default():
    assert llm.get_provider({}).name == llm.DEFAULT_KIND
    assert llm.get_provider(None).name == llm.DEFAULT_KIND


def test_describe_never_raises_even_for_nonsense():
    state = llm.describe({"provider": "no-such-provider"})
    assert state["available"] is False
    assert state["context_window"] is None
    assert "no-such-provider" in state["status"]


def test_describe_survives_a_provider_whose_probe_explodes(monkeypatch):
    """`brief doctor` must report every profile, not die on the first one."""

    class Exploding:
        name = "boom"

        def available(self):
            raise OSError("socket is on fire")

    monkeypatch.setattr(llm, "get_provider", lambda cfg: Exploding())
    state = llm.describe({"provider": "local"})
    assert state["available"] is False
    assert "socket is on fire" in state["status"]


def test_a_profiles_model_and_timeout_reach_the_provider(stub_clis):
    stub_clis.install("claude")
    provider = llm.get_provider(
        {"provider": "claude-cli", "model": "some-model", "timeout_s": 42}
    )
    assert getattr(provider, "model", None) == "some-model"
    assert getattr(provider, "timeout", None) == 42


def test_the_local_provider_takes_its_base_url_from_config():
    provider = llm.get_provider(
        {"provider": "local", "base_url": "http://gpu-box.invalid:11434"}
    )
    assert "gpu-box.invalid" in getattr(provider, "base_url", "")
    assert provider.available() is False, (
        "an unreachable server is a state, not a crash"
    )
    assert provider.status(), "and it still explains itself"


def test_synthesize_imports_no_vendor_sdk():
    """CLAUDE.md rule 6, checked as a grep because it is about what is absent."""
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parent.parent
        / "src"
        / "brief"
        / "synthesize.py"
    ).read_text(encoding="utf-8")
    for banned in (
        "import anthropic",
        "from anthropic",
        "import openai",
        "cli_llm_providers",
        "ollama",
    ):
        assert banned not in source, f"synthesize.py must not know about {banned}"


# --- reproducibility -------------------------------------------------------


def test_the_local_provider_defaults_to_a_reproducible_sampler():
    """Ollama defaults to temperature 0.8 and a fresh random seed.

    Measured on a 27B model: without these, two identical calls invented two
    different award titles. With them, five of six runs of the real prompt were
    byte-identical and the sixth differed by one punctuation mark — Metal is not
    bit-deterministic, so this buys stability rather than a guarantee. The
    facts, citations and amounts were constant across all six.
    """
    provider = llm.get_provider({"provider": "local"})
    assert provider.options["temperature"] == 0
    assert provider.options["seed"] == 42
    assert provider.options["top_k"] == 1


def test_a_profile_may_choose_its_own_seed():
    provider = llm.get_provider({"provider": "local", "seed": 7})
    assert provider.options["seed"] == 7


def test_a_null_seed_opts_back_out_of_reproducibility():
    """Someone who wants variety should be able to have it."""
    provider = llm.get_provider({"provider": "local", "seed": None})
    assert provider.options is None


def test_explicit_options_win_over_the_defaults():
    provider = llm.get_provider(
        {"provider": "local", "options": {"temperature": 0.7, "seed": 9}}
    )
    assert provider.options["temperature"] == 0.7
    assert provider.options["seed"] == 9


def test_keep_alive_reaches_the_provider():
    """A backfill pausing between weeks can otherwise lose an 18 GB model."""
    assert llm.get_provider({"provider": "local", "keep_alive": "30m"}).keep_alive == "30m"
    assert llm.get_provider({"provider": "local"}).keep_alive is None


def test_the_shipped_config_asks_for_a_reproducible_brief():
    import pathlib

    import yaml

    root = pathlib.Path(__file__).resolve().parent.parent
    cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))["llm"]
    assert cfg["seed"] is not None, (
        "a weekly report wants two runs over the same input to differ only when "
        "something real changed"
    )
    assert cfg["temperature"] == 0
    assert cfg["keep_alive"], "a backfill should not reload the model between weeks"


def test_the_cli_providers_ignore_sampler_options():
    """They hold their own settings; passing a seed must not break construction."""
    provider = llm.get_provider({"provider": "claude-cli", "seed": 42, "keep_alive": "30m"})
    assert provider.name == "claude-cli"
