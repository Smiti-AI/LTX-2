"""
Modes and model-role resolution.

A `Mode` is the user-facing dial: mock / cheap / pro / max.
A `ModelRole` is what the call site is doing: scene_agent, character_agent,
detection, text_reasoning, aggregator, comparator, prompt_reasoner.

`resolve_model(mode, role)` returns the concrete Gemini model ID for a
(mode, role) pair, or `None` if mode is mock (caller must short-circuit).

Pricing: see core.cost.PRICING_USD_PER_MTOK.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional


class Mode(str, Enum):
    MOCK = "mock"      # No API calls. Stub responses. $0.
    CHEAP = "cheap"    # Flash everything. Default.
    PRO = "pro"        # Pro for VLM agents, Flash for detection/text reasoning.
    MAX = "max"        # Pro everywhere + denser grids.

    @classmethod
    def parse(cls, value: str) -> "Mode":
        v = value.strip().lower()
        for m in cls:
            if m.value == v:
                return m
        raise ValueError(
            f"Unknown mode {value!r}. Valid: {', '.join(m.value for m in cls)}"
        )


class ModelRole(str, Enum):
    # Scene-level VLM agents (motion, env, rendering, audio, blind caption,
    # prompt fidelity).
    SCENE_AGENT = "scene_agent"
    # Per-character VLM agents (body, face, character consistency, topology).
    CHARACTER_AGENT = "character_agent"
    # Character bbox detection — always cheap, runs every mode (except mock).
    DETECTION = "detection"
    # Pure text reasoning (no video): speech coherence, prompt-vs-caption.
    TEXT_REASONING = "text_reasoning"
    # Final aggregator that consumes per-agent reports.
    AGGREGATOR = "aggregator"
    # Compare two completed reports — text-only, takes both report.json blobs.
    COMPARATOR = "comparator"
    # Optional deep-dive Prompt Reasoner.
    PROMPT_REASONER = "prompt_reasoner"


# Concrete model IDs.
_FLASH = "gemini-2.5-flash"
_PRO = "gemini-2.5-pro"


# (mode, role) -> model ID. None means mock-stub the call.
_TABLE: dict[tuple[Mode, ModelRole], Optional[str]] = {
    # ── MOCK: nothing runs.
    **{(Mode.MOCK, role): None for role in ModelRole},

    # ── CHEAP: Flash for everything.
    **{(Mode.CHEAP, role): _FLASH for role in ModelRole},

    # ── PRO: Pro for VLM/aggregator/comparator/reasoner; Flash for cheap roles.
    (Mode.PRO, ModelRole.SCENE_AGENT):     _PRO,
    (Mode.PRO, ModelRole.CHARACTER_AGENT): _PRO,
    (Mode.PRO, ModelRole.AGGREGATOR):      _PRO,
    (Mode.PRO, ModelRole.COMPARATOR):      _PRO,
    (Mode.PRO, ModelRole.PROMPT_REASONER): _PRO,
    (Mode.PRO, ModelRole.DETECTION):       _FLASH,
    (Mode.PRO, ModelRole.TEXT_REASONING):  _FLASH,

    # ── MAX: Pro everywhere.
    **{(Mode.MAX, role): _PRO for role in ModelRole},
}


def resolve_model(mode: Mode, role: ModelRole) -> Optional[str]:
    """Return the concrete model ID, or None if the call must be mocked."""
    return _TABLE[(mode, role)]


# Grid density per mode — `max` packs more panels for the per-character grids.
# Other modes share the V3 default (24 panels, 4 cols).
_GRID_DENSITY: dict[Mode, dict[str, int]] = {
    Mode.MOCK:  {"per_char_panels": 24, "per_char_cols": 4, "keyframes": 24},
    Mode.CHEAP: {"per_char_panels": 24, "per_char_cols": 4, "keyframes": 24},
    Mode.PRO:   {"per_char_panels": 24, "per_char_cols": 4, "keyframes": 24},
    Mode.MAX:   {"per_char_panels": 36, "per_char_cols": 6, "keyframes": 32},
}


def grid_density(mode: Mode) -> dict[str, int]:
    return dict(_GRID_DENSITY[mode])
