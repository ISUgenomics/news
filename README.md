# topic-brief

Weekly one-page digests of public activity on a chosen topic, for a chosen audience.

A pipeline plus a set of **profiles**. A profile says what the topic is, which sources to watch,
how to decide relevance, how to organize the page, and who receives it. The pipeline is the same
for every profile. The first profile is a brief on AI activity at Iowa State.

```
uv sync

# Secrets: the keychain is preferred on macOS, so nothing lands in a file.
security add-generic-password -s topic-brief -a CD_TOKEN -w

uv run brief doctor                       # which LLM providers are reachable
uv run brief ingest                       # all sources referenced by any enabled profile
uv run brief synthesize --profile isu-ai
uv run brief deliver    --profile isu-ai
```

The LLM is reached through a pluggable provider: a local Ollama model (the default), a
coding CLI you are already signed in to (`claude`, `codex`), or a metered API. Switching
is one line in `config.yaml` or in a profile.

Local is the default because it asks nothing of anyone — no credential, no token expiry,
and no question about whether an unattended weekly job is a permitted use of a
coding-CLI subscription. Name the model explicitly; auto-selection picks the largest
model at or under 15B, which is a rule for interactive apps that need fast first tokens,
not for a weekly batch job where two minutes is free and quality is everything.

- `spec.md` — the product and architecture spec
- `BUILD-SPEC.md` — how it was built, and the decisions behind it
- `CLAUDE.md` — the non-negotiables an agent must not violate
- `docs/seeds/` — the approved boundary for each extractable module
