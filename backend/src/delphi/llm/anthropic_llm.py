"""The Anthropic adapter of the LLM port (docs/15 §2–3, #104 part 2).

One Messages API request per call, configured per docs/15 §3:

- **model** from settings (``DELPHI_LLM_MODEL``, default ``claude-opus-5-5``); no ``thinking``
  parameter (adaptive by default on this model and it cannot be disabled), no sampling
  parameters, no prefill;
- **effort** set explicitly, by purpose (the model's default is ``medium``);
- **structured output** through ``output_config.format``. The JSON schema comes from the
  purpose's Pydantic model, with the constraints the API does not take (string lengths, array
  sizes, numeric bounds) left out of what is sent, and ``additionalProperties: false`` on every
  object. The reply is then validated against the full Pydantic model, which enforces those
  constraints client-side (what the SDK's ``parse`` helper does). docs/15 §5's validators run
  on top of that in ``LlmService``;
- **refusals**: ``stop_reason == "refusal"`` is checked before the content is read, and the
  server-side fallback is on (``fallbacks="default"``, beta ``server-side-fallback-2026-07-01``):
  a declined request is re-run on Anthropic's recommended model for that refusal category;
- **timeouts and retries**: ``timeout=20`` s, ``max_retries=2`` (the SDK retries
  408/409/429/5xx and connection errors).

The payload goes in as JSON inside ``<evidence>`` tags: untrusted data, never instructions
(docs/15 §4). Errors are reported by kind only, never with the payload or the key. The LLM never
decides anything (ADR-0004); its output is only text for people to read.
"""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Mapping
from typing import Any, Literal

import anthropic
from anthropic.types.beta import BetaOutputConfigParam, BetaUserMessageParam
from pydantic import BaseModel, ValidationError

from delphi.llm.port import LlmRequest, LlmResult, LlmStatus, Purpose

PROVIDER = "anthropic"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
TIMEOUT_S = 20.0  # docs/15 §3
MAX_RETRIES = 2  # docs/15 §3
# docs/15 §3: low for narratives and descriptions, medium for question variants.
EFFORT: Mapping[Purpose, Literal["low", "medium"]] = {
    Purpose.EVIDENCE_NARRATIVE: "low",
    Purpose.CASE_SUMMARY: "low",
    Purpose.TYPOLOGY_DESCRIPTION: "low",
    Purpose.QUESTION_VARIANTS: "medium",
}
# JSON Schema keywords that structured outputs does not take; enforced client-side instead.
_UNSUPPORTED = frozenset(
    {
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "pattern",
        "title",
    }
)
MS_PER_S = 1000


def output_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The JSON schema sent with the request: the Pydantic schema without the unsupported
    keywords, every object closed (``additionalProperties: false``, all properties required)."""

    def clean(node: Any) -> Any:
        if isinstance(node, dict):
            out = {k: clean(v) for k, v in node.items() if k not in _UNSUPPORTED}
            if out.get("type") == "object":
                out["additionalProperties"] = False
                if "properties" in out:
                    out["required"] = list(out["properties"])
            return out
        if isinstance(node, list):
            return [clean(v) for v in node]
        return node

    result: dict[str, Any] = clean(copy.deepcopy(model.model_json_schema()))
    return result


def user_message(payload: Mapping[str, Any]) -> str:
    """docs/15 §4: the payload as JSON inside <evidence> tags, last (cache-friendly)."""
    return f"<evidence>\n{json.dumps(payload, sort_keys=True, ensure_ascii=False)}\n</evidence>"


class AnthropicLlm:
    """``LlmPort`` over the official ``anthropic`` SDK."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self._model = model
        self._client = client or anthropic.Anthropic(
            api_key=api_key, timeout=TIMEOUT_S, max_retries=MAX_RETRIES
        )

    @property
    def name(self) -> str:
        return PROVIDER

    def generate(self, request: LlmRequest) -> LlmResult:
        started = time.perf_counter()

        def result(status: LlmStatus, **kw: Any) -> LlmResult:
            latency = int((time.perf_counter() - started) * MS_PER_S)
            return LlmResult(
                status,
                kw.pop("parsed", None),
                PROVIDER,
                kw.pop("model", self._model),
                latency_ms=latency,
                **kw,
            )

        message: BetaUserMessageParam = {
            "role": "user",
            "content": user_message(request.user_payload),
        }
        config: BetaOutputConfigParam = {
            "effort": EFFORT[request.purpose],
            "format": {"type": "json_schema", "schema": output_schema(request.output_schema)},
        }
        try:
            response = self._client.beta.messages.create(
                model=self._model,
                max_tokens=request.max_tokens,
                system=request.system,
                messages=[message],
                output_config=config,
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.APITimeoutError:
            return result(LlmStatus.ERROR, error="timeout")
        except anthropic.RateLimitError:
            return result(LlmStatus.ERROR, error="rate_limited")
        except anthropic.APIStatusError as exc:
            return result(LlmStatus.ERROR, error=f"http_{exc.status_code}")
        except anthropic.APIConnectionError:
            return result(LlmStatus.ERROR, error="connection")

        usage = response.usage
        tokens = {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}
        served_by = response.model  # after a server-side fallback, the model that answered
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details is not None else None
            return result(LlmStatus.REFUSED, model=served_by, error=f"refusal:{category}", **tokens)
        if response.stop_reason == "max_tokens":
            return result(LlmStatus.INVALID_OUTPUT, model=served_by, error="max_tokens", **tokens)
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            return result(LlmStatus.INVALID_OUTPUT, model=served_by, error="no_text", **tokens)
        try:
            parsed = request.output_schema.model_validate_json(text)
        except ValidationError:
            return result(LlmStatus.INVALID_OUTPUT, model=served_by, error="schema", **tokens)
        return result(LlmStatus.OK, parsed=parsed, model=served_by, **tokens)
