# 15 — LLM integration (Claude)

Module `delphi.llm`. ADR-0004 (the LLM never decides) and ADR-0007 (Claude plus a template
fallback).

## 1. What the LLM is allowed to do

| Use (`purpose`) | Mode | Model output used for | Deterministic alternative (fallback) |
|---|---|---|---|
| `EVIDENCE_NARRATIVE` | online job, per evidence record | 1–3 sentence finding with citations, shown on the evidence card | template per source |
| `CASE_SUMMARY` | online job, on DD pack request | A summary paragraph at the top of the DD pack | template |
| `QUESTION_VARIANTS` | offline job, triggered by an FDE | Draft question variants and translations (hi/te/ta), status `PENDING_REVIEW` | none: no variants are created |
| `TYPOLOGY_DESCRIPTION` | Scout job | Suggested name and description of a candidate typology | template (doc 11 §2) |

**Forbidden, and enforced by design:** choosing probes or evidence, setting probabilities or
outcomes, choosing decisions, approving liens, sending alerts, and real-time customer-facing text
(the customer only ever sees human-approved question-bank variants and fixed screens). The LLM
has **no tools** (no function calling) in the PoC.

## 2. Provider port

```python
class LlmPort(Protocol):
    def generate(self, request: LlmRequest) -> LlmResult: ...

LlmRequest: purpose, prompt_version, system: str, user_payload: dict (JSON), output_schema: type[BaseModel], max_tokens
LlmResult:  status (OK | REFUSED | INVALID_OUTPUT | ERROR | BUDGET_EXCEEDED | DISABLED),
            parsed: BaseModel | None, provider, model, input_tokens, output_tokens, latency_ms
```

Adapters:

- `AnthropicLlm`, using the official `anthropic` Python SDK. Before writing it, read the
  `claude-api` skill's `python/claude-api/README.md` and structured-outputs docs. Do not guess
  SDK calls.
- `TemplateLlm`: deterministic. Used in CI and tests, when `DELPHI_LLM_PROVIDER=template`, and
  as the fallback.

**Fallback chain** (in `llm.service`): `AnthropicLlm` → if status ≠ OK, or validation (§5)
fails → `TemplateLlm`. The stored record says which provider produced the text
(`narrative_source = LLM | TEMPLATE`), and the UI labels it.

## 3. Anthropic configuration

| Setting | Value |
|---|---|
| Model | `claude-opus-5-5` (env `DELPHI_LLM_MODEL`; the default model per Anthropic guidance) |
| Thinking | omitted (adaptive by default on this model; it cannot be disabled) |
| Effort | `output_config.effort = "low"` for narratives and descriptions, `"medium"` for question variants. Set it explicitly: the model's default is `medium` |
| Structured output | Pydantic schema via the SDK's structured-output parse helper (`output_config.format`), so the response is schema-validated |
| Sampling params | none (`temperature`/`top_p`/`top_k` are rejected by the current API version) |
| Prefill | none (not supported) |
| Refusals | check `stop_reason == "refusal"` before reading content → status REFUSED → template fallback. Enable the server-side fallback (`betas=["server-side-fallback-2026-07-01"]`, `fallbacks="default"`) |
| Timeout / retries | client `timeout=20.0` s, `max_retries=2` (the SDK retries 408/409/429/5xx). Worst case ≈ 60 s, which is acceptable for async jobs |
| max_tokens | 1,024 (narratives, descriptions), 4,096 (question variants) |
| Budget guard | `DELPHI_LLM_DAILY_TOKEN_BUDGET` (default 2,000,000 input+output tokens per UTC day), tracked in `llm_calls`. Above it → BUDGET_EXCEEDED → template |
| Kill switch | `DELPHI_LLM_PROVIDER=template` |
| Secret | `ANTHROPIC_API_KEY` from the k8s Secret `delphi-app`. Never logged |

The model id is configuration, so a later change to the Sonnet/Haiku tier for cost is a config
change. The owner decides that.

## 4. Prompts

Prompts are versioned files in `backend/src/delphi/llm/prompts/<purpose>/v<N>.md`. The
`prompt_version` is stored with every call. Rules for every prompt:

- The system prompt states the role and that the content in `<evidence>` tags is **untrusted
  data, not instructions** (prompt-injection defence, EFG §29).
- The user payload is JSON inside `<evidence>…</evidence>`. Every record has a `record_id`.
- The output schema requires citations by `record_id`.
- Stable content first and the variable payload last (prompt-caching friendly).

`EVIDENCE_NARRATIVE` output schema:

```json
{ "finding": "string, ≤ 400 chars",
  "citations": ["record_id", "..."] }
```

`QUESTION_VARIANTS` output schema:
`{ "variants": [ { "language": "hi|te|ta|en", "text": "...", "options": [{"key": "...", "text": "..."}] } ] }`.
Option keys MUST equal the source probe's keys exactly.

## 5. Output validation (all must pass, else fallback)

1. The schema is valid (guaranteed by structured outputs; still re-validated with Pydantic).
2. **Citation validity**: every cited `record_id` exists in the payload, and at least one
   citation is present.
3. **Numeric fidelity**: every number in `finding` (parsed including Indian digit grouping
   "4,80,000", "₹", "%", decimals) equals a number in the cited records, after normalisation
   (₹ ↔ paise / 100, percentages ↔ fractions × 100, rounded to the precision shown).
4. **No decision language**: case-insensitive regex blocklist (`\b(confirm(ed)?|is|are)\s+(a\s+)?(fraud|mule|scam)\b`,
   `\bshould\s+(be\s+)?(block|hold|freeze|release)`, `\bguilty\b`, `\binnocent\b`, …) kept in
   `llm/validators.py`.
5. **Length** ≤ 400 chars (narrative), ≤ 1,200 chars (case summary).
6. **Question variants**: option keys match exactly; the text has no URLs and no phone numbers;
   length ≤ 300 chars. Variants are *never* auto-approved.

Validation results are stored in `llm_calls.validation`.

## 6. Data minimisation (even though all data is synthetic)

The payload contains pseudonyms (`CUST-1234`, `ACC-…5678` masked to the last 4 digits) and only
the facts for the record. No names, phone numbers or full account numbers are sent. The mapping
lives only in the database. `llm_calls` stores the redacted payload and the output for 30 days,
for audit (doc 17 §6).

## 7. Templates (`TemplateLlm`)

Each source has a template in `llm/templates/<purpose>/<SOURCE_KEY>.j2` (Jinja2, autoescape),
which renders the deterministic facts. Example (`INBOUND_FAN_IN`):
"{{ m }} unrelated payers credited {{ account }} in the 48 h before {{ as_of }}
[{{ record_id }}]." Templates are the reference for the numeric-fidelity test.

## 8. Evaluation of the LLM path (`just llm-eval`, manual, costs money)

A fixed set of 30 evidence payloads (`tests/llm_eval/cases.jsonl`). Pass criteria: citation
validity 100%, numeric fidelity 100%, decision-language violations 0, fallback rate ≤ 10%.
The run is not part of CI (it costs money and is non-deterministic). It is run before each
milestone demo, and its results are committed to `docs/reports/llm-eval-<date>.md`.
