"""
Cost tracking. Every paid model call is recorded as a CostEntry. Totals are
exposed on the report and on the comparator output so the user always sees
exactly what each run cost.

Pricing reflects Vertex AI Gemini 2.5 list rates. Update the table when
prices change — there is one source of truth.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# USD per 1M tokens. Vertex AI Gemini 2.5 (as of late 2025).
# Pro has a >200K-input tier — we conservatively use the higher rate
# everywhere so we never under-report.
PRICING_USD_PER_MTOK: dict[str, dict[str, float]] = {
    "gemini-2.5-pro":   {"input": 2.50, "output": 15.00},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Compute the dollar cost of a call from token counts. Returns 0.0 for
    unknown models so missing entries don't blow up reports — but they will
    be visible because the by_model breakdown shows $0 for that model."""
    rates = PRICING_USD_PER_MTOK.get(model)
    if rates is None:
        return 0.0
    return (input_tokens / 1_000_000) * rates["input"] + (
        output_tokens / 1_000_000
    ) * rates["output"]


class BudgetExceeded(RuntimeError):
    """Raised when accumulated cost crosses the per-run ceiling mid-flight."""


@dataclass
class CostEntry:
    """One paid-model call. Mock calls produce a CostEntry with cost=0,
    model='mock' so the calls list is still complete and auditable."""
    role: str          # ModelRole value, e.g. 'scene_agent'
    agent_id: str      # high-level identity, e.g. 'motion_weight' or 'body_consistency::pink dog'
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    elapsed_s: float
    timestamp: float   # epoch seconds

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CostTracker:
    """Thread-safe accumulator of CostEntry. One per run; passed via RunContext."""
    max_spend_usd: Optional[float] = None  # None = no ceiling (mock/cheap default)
    entries: list[CostEntry] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(
        self,
        *,
        role: str,
        agent_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        elapsed_s: float,
    ) -> CostEntry:
        cost = estimate_cost_usd(model, input_tokens, output_tokens)
        entry = CostEntry(
            role=role,
            agent_id=agent_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            elapsed_s=elapsed_s,
            timestamp=time.time(),
        )
        with self._lock:
            self.entries.append(entry)
            total = sum(e.cost_usd for e in self.entries)
        if self.max_spend_usd is not None and total > self.max_spend_usd:
            raise BudgetExceeded(
                f"Run exceeded max_spend_usd=${self.max_spend_usd:.2f} "
                f"after {len(self.entries)} call(s) totaling ${total:.4f}. "
                f"Last call: role={role} agent_id={agent_id} model={model} "
                f"cost=${cost:.4f}"
            )
        return entry

    def total_usd(self) -> float:
        with self._lock:
            return sum(e.cost_usd for e in self.entries)

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            entries = list(self.entries)
        by_model: dict[str, float] = {}
        by_agent: dict[str, float] = {}
        in_tok = 0
        out_tok = 0
        for e in entries:
            by_model[e.model] = by_model.get(e.model, 0.0) + e.cost_usd
            by_agent[e.agent_id] = by_agent.get(e.agent_id, 0.0) + e.cost_usd
            in_tok += e.input_tokens
            out_tok += e.output_tokens
        return {
            "total_usd": round(sum(by_model.values()), 6),
            "calls": len(entries),
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "by_model": {k: round(v, 6) for k, v in by_model.items()},
            "by_agent": {k: round(v, 6) for k, v in sorted(by_agent.items())},
            "max_spend_usd": self.max_spend_usd,
            "entries": [e.to_dict() for e in entries],
        }
