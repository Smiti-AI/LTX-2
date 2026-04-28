"""Core library — pure functions, no filesystem assumptions, no argparse.

Everything that touches a paid model goes through `core.llm.call_model`,
which enforces the lockdown layers in `core.safety` and routes the call
through `core.cost.CostTracker`.
"""
from .modes import Mode, ModelRole, resolve_model
from .safety import (
    ExpensiveModeDisabled,
    EXPENSIVE_MODES_DISABLED,
    require_mode_allowed,
)
from .cost import CostTracker, CostEntry, BudgetExceeded
from .context import RunContext
from .llm import call_model, MockResponse

__all__ = [
    "Mode",
    "ModelRole",
    "resolve_model",
    "ExpensiveModeDisabled",
    "EXPENSIVE_MODES_DISABLED",
    "require_mode_allowed",
    "CostTracker",
    "CostEntry",
    "BudgetExceeded",
    "RunContext",
    "call_model",
    "MockResponse",
]
