"""
Three-layer lockdown for expensive modes.

Why this exists: pro/max modes call gemini-2.5-pro on video, which costs
~$0.30-2.00 per video. We want the pipeline to be *physically incapable*
of running in those modes by accident. Each layer is independent — any one
of them blocks expensive runs even if the others are misconfigured.

Layer 1 — compile-time kill switch (this file)
Layer 2 — daily-rotating env unlock (this file)
Layer 3 — per-invocation flags (enforced by CLI / HTTP layer; this file
          provides the predicate)
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .modes import Mode


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — Compile-time kill switch
# ─────────────────────────────────────────────────────────────────────────────
# To re-enable expensive modes, flip this to False. Showing up in `git diff`
# is the point — every relaxation must be a deliberate, reviewed commit.
EXPENSIVE_MODES_DISABLED: bool = True

# Modes that are *always* allowed regardless of the kill switch.
# These make zero (mock) or only Flash-tier (cheap) API calls.
ALWAYS_ALLOWED_MODE_NAMES: frozenset[str] = frozenset({"mock", "cheap"})


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — Daily-rotating env unlock
# ─────────────────────────────────────────────────────────────────────────────
# Even if Layer 1 is flipped off, the runtime requires VLM_EXPENSIVE_UNLOCK
# to equal today's UTC date (YYYY-MM-DD). A stale value left in .env from
# yesterday fails. This prevents "set it once and forget".
ENV_UNLOCK_VAR: str = "VLM_EXPENSIVE_UNLOCK"


def _today_utc_date_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def env_unlock_status() -> tuple[bool, str]:
    """Returns (ok, reason). ok=True means the env unlock matches today's date."""
    val = os.environ.get(ENV_UNLOCK_VAR, "").strip()
    today = _today_utc_date_str()
    if not val:
        return False, f"{ENV_UNLOCK_VAR} not set (expected {today})"
    if val != today:
        return False, f"{ENV_UNLOCK_VAR}={val!r} does not match today {today!r}"
    return True, f"{ENV_UNLOCK_VAR} matches today {today}"


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3 — Per-invocation flags
# ─────────────────────────────────────────────────────────────────────────────
# Caller must pass accept_cost=True AND a positive max_spend_usd. The CLI
# wires --i-accept-cost and --max-spend-usd to these. The HTTP API does the
# same with body params. No defaults — the caller must be explicit.

class ExpensiveModeDisabled(RuntimeError):
    """Raised when an expensive mode is requested but lockdown blocks it."""


def require_mode_allowed(
    mode: "Mode",
    *,
    accept_cost: bool = False,
    max_spend_usd: float | None = None,
) -> None:
    """Run the 3 layers in order. Raises ExpensiveModeDisabled with a detailed
    message listing every layer's status if any layer blocks the mode.

    Cheap and mock modes are unconditionally allowed.
    """
    from .modes import Mode  # avoid circular import at module load

    if mode.value in ALWAYS_ALLOWED_MODE_NAMES:
        return

    # mode is pro or max from here on.
    layer_statuses: list[str] = []

    layer1_ok = not EXPENSIVE_MODES_DISABLED
    layer_statuses.append(
        f"  Layer 1 (code):     "
        f"EXPENSIVE_MODES_DISABLED={EXPENSIVE_MODES_DISABLED} "
        f"in vlm_pipeline/core/safety.py "
        f"{'[OK]' if layer1_ok else '[BLOCKED]'}"
    )

    layer2_ok, layer2_reason = env_unlock_status()
    layer_statuses.append(
        f"  Layer 2 (env):      {layer2_reason} "
        f"{'[OK]' if layer2_ok else '[BLOCKED]'}"
    )

    layer3_ok = bool(accept_cost) and (max_spend_usd is not None) and max_spend_usd > 0
    if not accept_cost:
        layer3_reason = "accept_cost=False (CLI: pass --i-accept-cost)"
    elif max_spend_usd is None:
        layer3_reason = "max_spend_usd not provided (CLI: pass --max-spend-usd N)"
    elif max_spend_usd <= 0:
        layer3_reason = f"max_spend_usd={max_spend_usd} must be > 0"
    else:
        layer3_reason = f"accept_cost=True, max_spend_usd=${max_spend_usd:.2f}"
    layer_statuses.append(
        f"  Layer 3 (flags):    {layer3_reason} "
        f"{'[OK]' if layer3_ok else '[BLOCKED]'}"
    )

    if layer1_ok and layer2_ok and layer3_ok:
        return

    raise ExpensiveModeDisabled(
        f"'{mode.value}' mode is currently locked.\n"
        + "\n".join(layer_statuses)
        + "\nTo unlock: see vlm_pipeline/core/safety.py"
    )
