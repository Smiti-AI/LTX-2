"""
FastAPI app exposing the pipeline as an HTTP service.

Endpoints:
    POST /jobs            multipart/form-data: video_a, video_b, mode, ...
    GET  /jobs/{job_id}   current status (and reports + comparison if done)
    GET  /jobs            list of recent jobs (debugging)
    GET  /                static frontend (two-upload UI)
    GET  /healthz         liveness

Mode resolution + lockdown layers run at submit time, so a request asking
for `pro` without the right env / flags fails fast with HTTP 403.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..core import ExpensiveModeDisabled, Mode
from .schemas import JobStatus
from .store import JobStore, default_jobs_dir
from .worker import JobRunner


_STATIC_DIR = Path(__file__).parent / "static"


def create_app(jobs_dir: Path | None = None) -> FastAPI:
    jobs_dir = jobs_dir or default_jobs_dir()
    store = JobStore(jobs_dir=jobs_dir)
    runner = JobRunner(store)

    app = FastAPI(title="VLM Video QA — Compare", version="0.1.0")

    # ── Static frontend ─────────────────────────────────────────────────────
    if _STATIC_DIR.exists():
        app.mount(
            "/static", StaticFiles(directory=str(_STATIC_DIR)), name="static",
        )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        idx = _STATIC_DIR / "index.html"
        if not idx.exists():
            raise HTTPException(status_code=404, detail="frontend not built")
        return FileResponse(idx)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "jobs_dir": str(store.jobs_dir)}

    # ── Job endpoints ───────────────────────────────────────────────────────

    @app.post("/jobs")
    async def create_job(
        video_a: UploadFile = File(...),
        video_b: UploadFile = File(...),
        mode: str = Form(...),
        prompt_a: str = Form(""),
        prompt_b: str = Form(""),
        label_a: str = Form("A"),
        label_b: str = Form("B"),
        accept_cost: bool = Form(False),
        max_spend_usd: float | None = Form(None),
    ) -> JSONResponse:
        try:
            mode_enum = Mode.parse(mode)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        # Read both videos fully into memory before persisting — these are
        # short clips (< 100MB typical), so this is fine for the dev build.
        bytes_a = await video_a.read()
        bytes_b = await video_b.read()
        if not bytes_a or not bytes_b:
            raise HTTPException(status_code=400, detail="empty upload")

        job_id = store.create(mode=mode_enum.value)
        store.save_video(job_id, "a", bytes_a)
        store.save_video(job_id, "b", bytes_b)
        store.save_prompt(job_id, "a", prompt_a)
        store.save_prompt(job_id, "b", prompt_b)

        try:
            runner.submit(
                job_id,
                mode=mode_enum,
                accept_cost=accept_cost,
                max_spend_usd=max_spend_usd,
                prompt_a=prompt_a,
                prompt_b=prompt_b,
                label_a=label_a,
                label_b=label_b,
            )
        except ExpensiveModeDisabled as exc:
            # Lockdown rejected the mode — return 403 with the layer breakdown.
            store.update_status(job_id, status="failed", error=str(exc))
            raise HTTPException(status_code=403, detail=str(exc)) from exc

        return JSONResponse({"job_id": job_id, "status": "queued"})

    @app.get("/jobs/{job_id}", response_model=JobStatus)
    def get_job(job_id: str) -> JobStatus:
        status = store.get_status(job_id)
        if status is None:
            raise HTTPException(status_code=404, detail="job not found")
        return status

    @app.get("/jobs")
    def list_jobs() -> dict:
        return {"jobs": store.list_jobs()}

    return app


def _main() -> None:
    """Entry point: `python -m vlm_pipeline.service`."""
    import uvicorn

    host = os.environ.get("VLM_HOST", "127.0.0.1")
    port = int(os.environ.get("VLM_PORT", "8765"))
    uvicorn.run(
        "vlm_pipeline.service.app:create_app",
        host=host, port=port,
        factory=True,
        reload=False,
    )


if __name__ == "__main__":
    _main()
