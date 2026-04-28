"""
Filesystem-backed job store. One directory per job under `jobs_dir`:

    <jobs_dir>/<job_id>/
        input_a.mp4
        input_b.mp4
        prompt_a.txt
        prompt_b.txt
        status.json          # current JobStatus snapshot
        report_a.json        # populated when video A finishes
        report_b.json        # populated when video B finishes
        comparison.json      # populated when comparator finishes

In-process locks keep concurrent reads/writes consistent. For multi-process
deployment, replace with a real DB (Postgres / Firestore) later — the
filesystem layout already mirrors what a blob-storage layout would be.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from .schemas import JobStatus


def _now() -> float:
    return time.time()


def _atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` via tempfile + os.replace().

    Path.write_text() truncates first, then writes — leaving a window where
    a concurrent reader sees an empty file (raises pydantic ValidationError
    -> HTTP 500). os.replace() is atomic on POSIX: the file path always
    points to either the old contents or the new contents, never empty.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


class JobStore:
    def __init__(self, jobs_dir: Path):
        self.jobs_dir = jobs_dir
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()

    def _lock_for(self, job_id: str) -> threading.Lock:
        with self._locks_lock:
            lk = self._locks.get(job_id)
            if lk is None:
                lk = threading.Lock()
                self._locks[job_id] = lk
            return lk

    def _job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    def create(self, *, mode: str) -> str:
        job_id = uuid.uuid4().hex[:16]
        d = self._job_dir(job_id)
        d.mkdir(parents=True, exist_ok=False)
        status = JobStatus(
            job_id=job_id, status="queued", mode=mode,
            created_at=_now(), updated_at=_now(),
        )
        self._write_status(job_id, status)
        return job_id

    def save_video(self, job_id: str, slot: str, raw_bytes: bytes) -> Path:
        assert slot in {"a", "b"}, slot
        path = self._job_dir(job_id) / f"input_{slot}.mp4"
        path.write_bytes(raw_bytes)
        return path

    def save_prompt(self, job_id: str, slot: str, text: str) -> Path:
        assert slot in {"a", "b"}, slot
        path = self._job_dir(job_id) / f"prompt_{slot}.txt"
        path.write_text(text)
        return path

    def video_path(self, job_id: str, slot: str) -> Path:
        return self._job_dir(job_id) / f"input_{slot}.mp4"

    def prompt_path(self, job_id: str, slot: str) -> Path:
        return self._job_dir(job_id) / f"prompt_{slot}.txt"

    # ── Status ──────────────────────────────────────────────────────────────

    def _status_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "status.json"

    def _write_status(self, job_id: str, status: JobStatus) -> None:
        with self._lock_for(job_id):
            _atomic_write_text(
                self._status_path(job_id),
                status.model_dump_json(indent=2),
            )

    def get_status(self, job_id: str) -> Optional[JobStatus]:
        path = self._status_path(job_id)
        if not path.exists():
            return None
        return JobStatus.model_validate_json(path.read_text())

    def update_status(self, job_id: str, **fields) -> JobStatus:
        with self._lock_for(job_id):
            current = self.get_status(job_id)
            if current is None:
                raise KeyError(job_id)
            data = current.model_dump()
            data.update(fields)
            data["updated_at"] = _now()
            new = JobStatus(**data)
            _atomic_write_text(self._status_path(job_id), new.model_dump_json(indent=2))
            return new

    def update_status_atomic(self, job_id: str, mutator) -> JobStatus:
        """Read-modify-write the status under the per-job lock. The mutator
        receives the current dict and may mutate it in place or return a new
        dict. Use this when two threads concurrently update overlapping
        fields (e.g. progress.a and progress.b) — `update_status(**fields)`
        replaces wholesale and would lose the other side's update."""
        with self._lock_for(job_id):
            current = self.get_status(job_id)
            if current is None:
                raise KeyError(job_id)
            data = current.model_dump()
            result = mutator(data)
            if result is not None:
                data = result
            data["updated_at"] = _now()
            new = JobStatus(**data)
            _atomic_write_text(self._status_path(job_id), new.model_dump_json(indent=2))
            return new

    # ── Report artefacts ────────────────────────────────────────────────────

    def write_report(self, job_id: str, slot: str, report: dict) -> None:
        assert slot in {"a", "b"}, slot
        path = self._job_dir(job_id) / f"report_{slot}.json"
        _atomic_write_text(path, json.dumps(report, ensure_ascii=False, indent=2))

    def write_comparison(self, job_id: str, comparison: dict) -> None:
        path = self._job_dir(job_id) / "comparison.json"
        _atomic_write_text(path, json.dumps(comparison, ensure_ascii=False, indent=2))

    def list_jobs(self) -> list[str]:
        return sorted(p.name for p in self.jobs_dir.iterdir() if p.is_dir())


def default_jobs_dir() -> Path:
    return Path(os.environ.get("VLM_JOBS_DIR", "/tmp/vlm_jobs")).resolve()
