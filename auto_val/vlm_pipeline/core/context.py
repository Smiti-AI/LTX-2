"""
RunContext — the bundle of (mode, cost_tracker, options) that every paid
call needs. Threaded explicitly through the pipeline (no global / threadlocal)
so concurrent runs in the same process are safe and the dependency is visible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .cost import CostTracker
from .modes import Mode


@dataclass
class RunContext:
    mode: Mode
    cost_tracker: CostTracker
    # Forwarded from the safety layer so downstream code can introspect.
    accept_cost: bool = False
    max_spend_usd: Optional[float] = None
    # Toggles
    enable_prompt_reasoning: bool = False

    @property
    def is_mock(self) -> bool:
        return self.mode is Mode.MOCK
