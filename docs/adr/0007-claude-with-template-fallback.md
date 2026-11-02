# ADR-0007 — Claude API behind a provider port, with a deterministic template fallback

- **Status:** Accepted
- **Date:** 2026-10-31

## Context
We need cited narratives, summaries, offline multilingual question drafts and typology
descriptions. The owner chose the Claude API with a fallback.

## Decision
`LlmPort` with two adapters:

- `AnthropicLlm`: the official `anthropic` SDK, model `claude-opus-5-5` by config, structured
  outputs, low effort, refusal handling with server-side fallback, 20 s timeout, 2 retries, and
  a daily token budget.
- `TemplateLlm`: Jinja2.

Every LLM output must pass the citation, numeric-fidelity and decision-language validators;
otherwise the template is used. Customer-facing questions are generated offline and approved by
humans.

## Alternatives considered
- Local model via Ollama: hardware-dependent, weaker multilingual quality (the owner declined).
- Templates only: less compelling narratives and translations.

## Consequences
- Positive: the demo never depends on the API (NFR-12). Costs are bounded.
- Negative: validators must be maintained as prompts evolve. Run `just llm-eval` before demos.
