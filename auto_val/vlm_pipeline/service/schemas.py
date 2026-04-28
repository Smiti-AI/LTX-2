"""Request / response schemas for the HTTP layer."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


class JobCreate(BaseModel):
    """Body for POST /jobs (form-encoded; videos come in as files).

    `mode` MUST be set explicitly by the caller — no default at the HTTP
    boundary so we never silently spend money. Cheap or mock are the only
    values the gate accepts unless the lockdown is opened (see core.safety).
    """
    mode: str
    prompt_a: str = ""
    prompt_b: str = ""
    label_a: str = "A"
    label_b: str = "B"
    accept_cost: bool = False
    max_spend_usd: Optional[float] = None


class JobStatus(BaseModel):
    job_id: str
    status: str           # "queued" | "running" | "done" | "failed"
    mode: str
    created_at: float
    updated_at: float
    progress: dict[str, Any] = {}   # {a: "running"|"done", b: "running"|"done", comparator: ...}
    error: Optional[str] = None

    # Populated when status == "done"
    report_a: Optional[dict] = None
    report_b: Optional[dict] = None
    comparison: Optional[dict] = None
    cost: Optional[dict] = None     # combined cost across both videos + comparator
