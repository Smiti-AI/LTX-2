"""
The single chokepoint for paid model calls.

Every Gemini call in the pipeline goes through `call_model`. It:
  1. Resolves (mode, role) -> model ID via `core.modes.resolve_model`.
  2. If the resolved model is None (mock mode), returns a stubbed response
     without constructing a client or hitting the network.
  3. Otherwise calls `client.models.generate_content` and records token
     usage on the `RunContext.cost_tracker`.

Retries on transient errors (499/503/504/CANCELLED/UNAVAILABLE/DEADLINE) up
to 3 attempts with exponential backoff — same policy as the original code,
just centralised.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from google import genai
from google.genai import types

from .context import RunContext
from .cost import CostTracker
from .modes import ModelRole, resolve_model


# ─────────────────────────────────────────────────────────────────────────────
# Shared Gemini client (Vertex AI). Constructed lazily, cached process-wide.
# ─────────────────────────────────────────────────────────────────────────────

_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
_LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
_USE_VERTEX = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "True").lower() in {
    "1", "true", "yes",
}

_cached_client: Optional[genai.Client] = None
_client_lock = threading.Lock()


def get_client() -> genai.Client:
    global _cached_client
    if _cached_client is not None:
        return _cached_client
    with _client_lock:
        if _cached_client is None:
            if _USE_VERTEX:
                if not _PROJECT:
                    raise RuntimeError(
                        "GOOGLE_CLOUD_PROJECT is not set. Set it (and "
                        "GOOGLE_APPLICATION_CREDENTIALS) in your environment "
                        "or in a .env file before running. See .env.example."
                    )
                _cached_client = genai.Client(
                    vertexai=True, project=_PROJECT, location=_LOCATION
                )
            else:
                _cached_client = genai.Client()
    return _cached_client


def prewarm_client() -> None:
    get_client()


# ─────────────────────────────────────────────────────────────────────────────
# Mock response — what every agent gets back in mode=mock.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MockResponse:
    """Stand-in for `google.genai.types.GenerateContentResponse`. Has just
    enough surface area for the parsing path: a `.text` attribute and a
    `.usage_metadata` with zero tokens."""
    text: str
    input_tokens: int = 0
    output_tokens: int = 0

    class _UsageMetadata:
        def __init__(self, in_tok: int = 0, out_tok: int = 0):
            self.prompt_token_count = in_tok
            self.candidates_token_count = out_tok

    @property
    def usage_metadata(self) -> "MockResponse._UsageMetadata":
        return MockResponse._UsageMetadata(self.input_tokens, self.output_tokens)


_MOCK_AGENT_JSON = (
    '{"verdict": "great", '
    '"summary": "Mock pipeline run \\u2014 no model was called.", '
    '"findings": "This response was produced by mode=mock. No video frames '
    'were sent, no Gemini call was made, no cost was incurred. The pipeline '
    'ran end-to-end so callers can verify integration shape, but every '
    'verdict here is a stub. Switch to mode=cheap to get real output."}'
)

_MOCK_AGGREGATOR_JSON = (
    '{"overall_verdict": "PASS", "score": 0, '
    '"headline": "Mock run \\u2014 no model was called.", '
    '"what_works": "(mock)", "what_breaks": "(mock)", '
    '"top_issue": "Mode is mock; switch to cheap or higher to get real verdicts."}'
)

_MOCK_COMPARATOR_JSON = (
    '{"winner": "tie", "confidence": "low", '
    '"headline": "Mock comparator \\u2014 no model was called.", '
    '"reasoning": "(mock)", "per_axis": {}}'
)

_MOCK_REASONER_JSON = (
    '{"skipped_reason": "mode=mock", "rationale": "", '
    '"diagnosed_conflicts": [], "trigger_attributions": [], '
    '"counterfactual_edits": [], "rewritten_prompt": ""}'
)


def _mock_response_for_role(role: ModelRole) -> MockResponse:
    if role is ModelRole.AGGREGATOR:
        return MockResponse(text=_MOCK_AGGREGATOR_JSON)
    if role is ModelRole.COMPARATOR:
        return MockResponse(text=_MOCK_COMPARATOR_JSON)
    if role is ModelRole.PROMPT_REASONER:
        return MockResponse(text=_MOCK_REASONER_JSON)
    # Default: agent-shaped response.
    return MockResponse(text=_MOCK_AGENT_JSON)


# ─────────────────────────────────────────────────────────────────────────────
# The wrapped call.
# ─────────────────────────────────────────────────────────────────────────────

_RETRYABLE_TOKENS = ("499", "503", "504", "CANCELLED", "UNAVAILABLE", "DEADLINE")


def call_model(
    *,
    ctx: RunContext,
    role: ModelRole,
    agent_id: str,
    contents: list[Any] | str,
    config: Optional[types.GenerateContentConfig] = None,
    max_attempts: int = 3,
) -> Any:
    """The one and only paid-model entry point.

    Returns whatever the underlying SDK returns (or a MockResponse). Caller is
    responsible for parsing `.text` — keeping that contract identical to the
    raw SDK keeps existing parsing code unchanged.

    Records a CostEntry on `ctx.cost_tracker` for every attempt that returns
    successfully, including mock calls (cost=$0, model='mock').
    """
    model_id = resolve_model(ctx.mode, role)
    t0 = time.time()

    # ── Mock short-circuit: never construct a client, never hit the network.
    if model_id is None:
        resp = _mock_response_for_role(role)
        ctx.cost_tracker.record(
            role=role.value,
            agent_id=agent_id,
            model="mock",
            input_tokens=0,
            output_tokens=0,
            elapsed_s=round(time.time() - t0, 3),
        )
        return resp

    cfg = config or types.GenerateContentConfig(
        temperature=0.1,
        response_mime_type="application/json",
        http_options=types.HttpOptions(timeout=240_000),
    )

    last_exc: Optional[BaseException] = None
    for attempt in range(max_attempts):
        try:
            resp = get_client().models.generate_content(
                model=model_id, contents=contents, config=cfg,
            )
            usage = getattr(resp, "usage_metadata", None)
            in_tok = int(getattr(usage, "prompt_token_count", 0) or 0)
            out_tok = int(getattr(usage, "candidates_token_count", 0) or 0)
            ctx.cost_tracker.record(
                role=role.value,
                agent_id=agent_id,
                model=model_id,
                input_tokens=in_tok,
                output_tokens=out_tok,
                elapsed_s=round(time.time() - t0, 3),
            )
            return resp
        except Exception as exc:
            last_exc = exc
            if any(tok in str(exc) for tok in _RETRYABLE_TOKENS) and attempt < max_attempts - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    assert last_exc is not None
    raise last_exc


__all__ = [
    "call_model",
    "get_client",
    "prewarm_client",
    "MockResponse",
]
