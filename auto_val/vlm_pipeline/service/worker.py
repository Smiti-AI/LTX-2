"""
Background job runner. One JobRunner per process; submit() enqueues onto an
internal ThreadPoolExecutor that runs each job to completion.

Each job:
  1. Run the pipeline on video A and video B in parallel (own RunContext each
     so cost is tracked per-video; we also build a shared cost summary).
  2. Run the comparator on the two reports.
  3. Persist report_a.json, report_b.json, comparison.json.
  4. Update the JobStatus to "done" or "failed".
"""
from __future__ import annotations

import concurrent.futures as cf
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from loguru import logger

from .. import agents as A
from .. import run as runmod
from ..core import (
    CostTracker,
    Mode,
    RunContext,
    require_mode_allowed,
)
from .store import JobStore


def _aggregate_cost(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum cost dicts across multiple per-call CostTracker.to_dict() outputs."""
    total = 0.0
    calls = 0
    in_tok = 0
    out_tok = 0
    by_model: dict[str, float] = {}
    by_agent: dict[str, float] = {}
    for c in parts:
        if not c:
            continue
        total += float(c.get("total_usd", 0.0))
        calls += int(c.get("calls", 0))
        in_tok += int(c.get("input_tokens", 0))
        out_tok += int(c.get("output_tokens", 0))
        for k, v in (c.get("by_model") or {}).items():
            by_model[k] = by_model.get(k, 0.0) + float(v)
        for k, v in (c.get("by_agent") or {}).items():
            by_agent[k] = by_agent.get(k, 0.0) + float(v)
    return {
        "total_usd": round(total, 6),
        "calls": calls,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "by_model": {k: round(v, 6) for k, v in by_model.items()},
        "by_agent": {k: round(v, 6) for k, v in sorted(by_agent.items())},
    }


class JobRunner:
    def __init__(self, store: JobStore, *, max_concurrent_jobs: int = 2):
        self.store = store
        # Outer pool: serialises jobs (so we don't spawn unbounded GPU/network usage).
        # Inner pool inside the pipeline already parallelises the agents.
        self._pool = cf.ThreadPoolExecutor(
            max_workers=max_concurrent_jobs,
            thread_name_prefix="vlm-job",
        )
        self._inflight: dict[str, cf.Future] = {}
        self._inflight_lock = threading.Lock()

    def submit(
        self,
        job_id: str,
        *,
        mode: Mode,
        accept_cost: bool,
        max_spend_usd: float | None,
        prompt_a: str,
        prompt_b: str,
        label_a: str,
        label_b: str,
    ) -> None:
        # Validate at submit time so a bad mode fails fast (before queueing).
        require_mode_allowed(mode, accept_cost=accept_cost, max_spend_usd=max_spend_usd)
        fut = self._pool.submit(
            self._run_job, job_id, mode, accept_cost, max_spend_usd,
            prompt_a, prompt_b, label_a, label_b,
        )
        with self._inflight_lock:
            self._inflight[job_id] = fut

    def _run_job(
        self,
        job_id: str,
        mode: Mode,
        accept_cost: bool,
        max_spend_usd: float | None,
        prompt_a: str,
        prompt_b: str,
        label_a: str,
        label_b: str,
    ) -> None:
        try:
            initial_progress = {
                "a": _initial_side_progress(),
                "b": _initial_side_progress(),
                "comparator": {"status": "queued"},
            }
            self.store.update_status(
                job_id, status="running", progress=initial_progress,
            )

            video_a = self.store.video_path(job_id, "a")
            video_b = self.store.video_path(job_id, "b")
            scratch_a = video_a.parent / "scratch_a"
            scratch_b = video_b.parent / "scratch_b"
            scratch_a.mkdir(exist_ok=True)
            scratch_b.mkdir(exist_ok=True)

            ctx_a = RunContext(
                mode=mode,
                cost_tracker=CostTracker(max_spend_usd=max_spend_usd),
                accept_cost=accept_cost, max_spend_usd=max_spend_usd,
            )
            ctx_b = RunContext(
                mode=mode,
                cost_tracker=CostTracker(max_spend_usd=max_spend_usd),
                accept_cost=accept_cost, max_spend_usd=max_spend_usd,
            )

            store = self.store

            def _make_handler(slot: str, t_started: float):
                def handle(evt: dict) -> None:
                    def mutator(s: dict) -> dict:
                        progress = dict(s.get("progress") or {})
                        side = dict(progress.get(slot) or _initial_side_progress())
                        agents = dict(side.get("agents") or {})
                        side["elapsed_s"] = round(time.time() - t_started, 1)
                        side["t_started"] = t_started
                        t = evt.get("type")
                        if t == "stage":
                            side["stage"] = evt["stage"]
                        elif t == "agent_started":
                            agents[evt["agent_id"]] = {
                                "status": "running",
                                "title": evt.get("title", evt["agent_id"]),
                            }
                        elif t == "agent_done":
                            agents[evt["agent_id"]] = {
                                "status": "done",
                                "title": evt.get("title", evt["agent_id"]),
                                "verdict": evt.get("verdict", "great"),
                                "summary": evt.get("summary", ""),
                                "elapsed_s": evt.get("elapsed_s", 0.0),
                            }
                        side["agents"] = agents
                        progress[slot] = side
                        s["progress"] = progress
                        return s
                    store.update_status_atomic(job_id, mutator)
                return handle

            # Run both videos through the pipeline in parallel.
            # Each pipeline already parallelises its own agents — we just
            # let the two top-level runs overlap so wall-clock is roughly
            # max(t_a, t_b) instead of t_a + t_b.
            with cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="vlm-pair") as pair_pool:
                def _run_side(slot: str, video: Path, prompt: str, ctx: RunContext, scratch: Path):
                    t0 = time.time()
                    handler = _make_handler(slot, t0)
                    handler({"type": "stage", "stage": "starting"})
                    payload = runmod.run_video(
                        str(video), prompt, scratch, ctx=ctx, on_event=handler,
                    )
                    self.store.write_report(job_id, slot, payload)
                    handler({"type": "stage", "stage": "done"})
                    return payload

                fa = pair_pool.submit(_run_side, "a", video_a, prompt_a, ctx_a, scratch_a)
                fb = pair_pool.submit(_run_side, "b", video_b, prompt_b, ctx_b, scratch_b)
                report_a = fa.result()
                report_b = fb.result()

            # Comparator — runs on a fresh tracker so its cost is attributable.
            self.store.update_status_atomic(
                job_id,
                lambda s: _set_in(s, ["progress", "comparator", "status"], "running"),
            )
            cmp_ctx = RunContext(
                mode=mode,
                cost_tracker=CostTracker(max_spend_usd=max_spend_usd),
                accept_cost=accept_cost, max_spend_usd=max_spend_usd,
            )
            comparison = A.run_comparator(
                report_a, report_b,
                ctx=cmp_ctx, label_a=label_a, label_b=label_b,
            )
            comparison["cost"] = cmp_ctx.cost_tracker.to_dict()
            self.store.write_comparison(job_id, comparison)

            combined_cost = _aggregate_cost([
                report_a.get("cost", {}),
                report_b.get("cost", {}),
                comparison.get("cost", {}),
            ])

            self.store.update_status_atomic(
                job_id,
                lambda s: _merge_status(s, status="done",
                    progress_patch={"comparator": {"status": "done"}},
                    report_a=report_a, report_b=report_b,
                    comparison=comparison, cost=combined_cost),
            )
        except Exception as exc:
            logger.exception(f"Job {job_id} failed: {exc}")
            self.store.update_status(
                job_id, status="failed",
                error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
            )


def _initial_side_progress() -> dict:
    return {"stage": "queued", "agents": {}, "elapsed_s": 0.0, "t_started": None}


def _set_in(s: dict, path: list[str], value) -> dict:
    """Mutator helper: set a nested value in `s`."""
    cur = s
    for k in path[:-1]:
        cur = cur.setdefault(k, {}) if isinstance(cur.get(k), dict) or k not in cur else cur[k]
        if not isinstance(cur, dict):
            return s
    cur[path[-1]] = value
    return s


def _merge_status(s: dict, *, progress_patch: dict | None = None, **fields) -> dict:
    """Mutator helper: deep-merge the progress dict, replace top-level fields."""
    if progress_patch:
        progress = dict(s.get("progress") or {})
        for k, v in progress_patch.items():
            existing = progress.get(k)
            if isinstance(existing, dict) and isinstance(v, dict):
                merged = dict(existing); merged.update(v); progress[k] = merged
            else:
                progress[k] = v
        s["progress"] = progress
    for k, v in fields.items():
        s[k] = v
    return s
