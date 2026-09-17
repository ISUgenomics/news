# topic-brief

Weekly one-page digests of public activity on a chosen topic, for a chosen audience.

A pipeline plus a set of **profiles**. A profile says what the topic is, which sources to watch,
how to decide relevance, how to organize the page, and who receives it. The pipeline is the same
for every profile. The first profile is a brief on AI activity at Iowa State.

```
uv sync
uv run brief doctor                       # which LLM providers are reachable
uv run brief ingest                       # all sources referenced by any enabled profile
uv run brief synthesize --profile isu-ai
uv run brief deliver    --profile isu-ai
```

The LLM is reached through a pluggable provider: a coding CLI you are already signed in to
(`claude`, `codex`), a local Ollama server, or a metered API. Switching is one line in
`config.yaml` or in a profile.

- `spec.md` — the product and architecture spec
- `BUILD-SPEC.md` — how it was built, and the decisions behind it
- `CLAUDE.md` — the non-negotiables an agent must not violate
- `docs/seeds/` — the approved boundary for each extractable module
