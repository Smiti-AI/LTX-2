#!/usr/bin/env python3
"""
VLM Video QA Pipeline — 10 Gemini 2.5 Pro agents on a video, heavily
parallelised: grid-independent agents and Gemini-Flash character detection
run concurrently, so grid-dependent agents start as soon as detection returns.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from loguru import logger

from . import agents as A
from . import detection as DET
from . import keyframes as KF
from . import lipsync_flow as LSF
from . import stabilisation as STAB
from .core import (
    CostTracker,
    Mode,
    RunContext,
    require_mode_allowed,
)
from .perception import transcribe_with_segments


# Default mode for both CLI and library callers. `cheap` runs Flash for every
# agent — pro/max require explicit opt-in plus the lockdown layers in core.safety.
DEFAULT_MODE = Mode.CHEAP


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="VLM Video QA Pipeline")
    p.add_argument("video_path")
    p.add_argument("--prompt", default="")
    p.add_argument("--prompt-file", default="")
    p.add_argument("--out-dir", default="./outputs_vlm")

    # Cost / mode controls.
    # `pro` and `max` are intentionally NOT in `choices` — they require both
    # --i-accept-cost and --max-spend-usd, plus passing the lockdown layers.
    p.add_argument(
        "--mode",
        choices=["mock", "cheap", "pro", "max"],
        default=DEFAULT_MODE.value,
        help="mock = no API calls (free, returns stub). "
             "cheap = Flash everything (default, ~$0.10/run). "
             "pro = Pro for VLM agents (locked, requires --i-accept-cost + "
             "--max-spend-usd + safety env unlock). "
             "max = Pro everywhere with denser grids (same gate as pro).",
    )
    p.add_argument(
        "--i-accept-cost",
        action="store_true",
        help="Required for pro/max. Acknowledges that the run will incur "
             "per-call charges on the configured GCP project.",
    )
    p.add_argument(
        "--max-spend-usd",
        type=float,
        default=None,
        help="Required for pro/max. Hard ceiling on accumulated cost; the "
             "run aborts mid-flight if exceeded.",
    )

    p.add_argument(
        "--enable-prompt-reasoning",
        action="store_true",
        help="Run the downstream Prompt Reasoning Agent (Deduction Engine) to "
             "propose a rewritten prompt that removes semantic triggers behind "
             "the observed failures. Off by default; adds one Gemini call "
             "after the aggregator.",
    )
    return p.parse_args()


# Agents that need nothing from character detection — they can start immediately,
# overlapping with the Flash detection call.
GRID_INDEPENDENT = {
    "motion_weight",
    "audio_quality",
    "speech_coherence",
    "blind_captioner",    # pure video-in, no grids / detection
    "prompt_fidelity",    # video + transcript + prompt text, no grid needed
}


def run_video(
    video_path: str,
    prompt_text: str,
    out_dir: Path,
    *,
    ctx: RunContext,
    on_event=None,
) -> dict:
    """Run the full pipeline on one video. The RunContext determines which
    models (if any) are called and accumulates cost. Use `build_run_context`
    or invoke from `main()` to construct one with the safety layers enforced.

    `on_event` is an optional callback `(dict) -> None` that fires for major
    stage transitions and per-agent start/done events. Used by the HTTP
    worker to surface live progress in the JobStatus. Event shapes:
      {"type": "stage", "stage": "transcribing"|"extracting_frames"|
                                 "detecting_characters"|"stabilising"|
                                 "running_agents"|"running_lipsync"|
                                 "running_pvc"|"aggregating"|"done"}
      {"type": "agents_planned", "items": [{"agent_id", "title"}, ...]}
      {"type": "agent_started", "agent_id"}
      {"type": "agent_done", "agent_id", "title", "verdict",
                             "summary", "elapsed_s"}
    """
    enable_prompt_reasoning = ctx.enable_prompt_reasoning
    video_path = os.path.abspath(video_path)

    def _emit(evt: dict) -> None:
        if on_event is None:
            return
        try:
            on_event(evt)
        except Exception:
            # Progress reporting must never break the run.
            logger.exception("on_event handler raised; ignoring")
    if not os.path.isfile(video_path):
        raise FileNotFoundError(video_path)

    stem = Path(video_path).stem
    tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = out_dir / f"{stem[:40]}__{tag}"
    run_dir.mkdir(parents=True, exist_ok=True)

    local_video = run_dir / Path(video_path).name
    shutil.copy2(video_path, local_video)

    t_total_start = time.time()

    # ── Perception: Whisper transcript + timestamped segments ───────────────
    _emit({"type": "stage", "stage": "transcribing"})
    logger.info("Transcribing audio …")
    transcript, segments = transcribe_with_segments(str(local_video))
    logger.info(f"Transcript: {transcript[:120]!r} ({len(segments)} segments)")

    # ── Extract 24 keyframes + up to 4×2 speech-pair frames (all at once) ───
    # Denser sampling catches short-duration defects (eye morphs, flicker,
    # small object teleports) that sparser grids averaged over.
    _emit({"type": "stage", "stage": "extracting_frames"})
    logger.info("Extracting 24 keyframes + speech-pair frames …")
    frames16 = KF.extract_evenly_spaced(str(local_video), n=24)
    chosen_segs = segments[:4] if segments else []
    speech_frames_flat: list = []
    for s in chosen_segs:
        t_start = s.start + (s.end - s.start) * 0.15
        t_end   = s.start + (s.end - s.start) * 0.85
        speech_frames_flat.extend(KF.extract_at_timestamps(str(local_video), [t_start, t_end]))
    all_frames = frames16 + speech_frames_flat

    speech_captions: list[str] = [s.text.strip()[:80] for s in chosen_segs]

    if segments:
        segments_block = "\n".join(
            f"  [{s.start:.2f}s – {s.end:.2f}s] {s.text.strip()}" for s in segments
        )
    else:
        segments_block = "(no segments)"

    # Read video bytes (used by every agent)
    with open(local_video, "rb") as f:
        video_bytes = f.read()

    A.prewarm_client()

    # ── PARALLEL PHASE ──────────────────────────────────────────────────────
    # 1. Start Gemini-Flash detection on ALL frames (main + speech) in one batch.
    # 2. Launch scene-level grid-independent agents immediately.
    # 3. Once detection returns: consolidate character names → group bboxes by
    #    canonical character → build per-character body + face grids →
    #    launch 3 × N per-character agents + 3 scene-level grid-dependent agents.
    # 4. Collect everything.

    reports_lock = threading.Lock()
    reports: list[A.AgentReport] = []

    pool = cf.ThreadPoolExecutor(max_workers=24)
    agent_futures: dict = {}

    # Human-readable labels for the checklist UI. Per-character agents append
    # the character name, e.g. "Body Consistency — pink dog".
    _SCENE_TITLES = {
        "motion_weight": "Motion & Weight",
        "environment_stability": "Environment Stability",
        "rendering_defects": "Rendering Defects",
        "audio_quality": "Audio Quality",
        "speech_coherence": "Speech & Dialogue Coherence",
        "blind_captioner": "Blind Captioner",
        "prompt_fidelity": "Prompt Fidelity",
    }
    _PER_CHAR_TITLES = {
        "body_consistency": "Body Consistency",
        "face_uncanny": "Face & Uncanny Valley",
        "character_consistency_audit": "Character Consistency Audit",
        "facial_topology_audit": "Facial Topology Audit",
    }

    def _agent_title(key: str) -> str:
        if "::" in key:
            base, cname = key.split("::", 1)
            return f"{_PER_CHAR_TITLES.get(base, base)} — {cname}"
        return _SCENE_TITLES.get(key, key)

    def _submit(name, fn, kwargs):
        _emit({"type": "agent_started", "agent_id": name, "title": _agent_title(name)})
        agent_futures[pool.submit(_runner, name, fn, kwargs)] = name

    def _runner(name, fn, kwargs):
        try:
            return fn(video_bytes, ctx=ctx, **kwargs)
        except Exception as exc:
            logger.error(f"{name} failed: {exc}")
            return A.AgentReport(
                agent_id=name, title=_agent_title(name), focus="",
                verdict="great", summary=f"(agent error: {exc})",
                findings=str(exc), elapsed_s=0.0,
            )

    # Prompt-dependent agents (Prompt Fidelity, Prompt vs Blind Caption) need
    # an actual prompt to compare against — without one they hallucinate a
    # verdict against an empty string. Skip them entirely when no prompt is
    # provided so the user doesn't see a fabricated "major issue".
    has_prompt = bool(prompt_text and prompt_text.strip())

    # Submit scene-level grid-independent agents first (parallel with detection).
    scene_indep_kwargs_pool = {
        "transcript":     transcript,
        "segments_block": segments_block,
        "prompt_text":    prompt_text,
    }
    for name, fn, needs in A.SCENE_AGENTS:
        if name in GRID_INDEPENDENT:
            if name == "prompt_fidelity" and not has_prompt:
                logger.info("Skipping Prompt Fidelity — no prompt provided.")
                continue
            kwargs = {k: scene_indep_kwargs_pool[k] for k in needs}
            _submit(name, fn, kwargs)

    # Start Flash detection in parallel
    _emit({"type": "stage", "stage": "detecting_characters"})
    t_det0 = time.time()
    logger.info(f"Starting Flash detection on {len(all_frames)} frames (parallel with {len(agent_futures)} agents) …")
    det_future = pool.submit(DET.detect_batch, all_frames, 16, ctx=ctx)

    bboxes_per_frame = det_future.result()
    main_bboxes = bboxes_per_frame[:len(frames16)]
    speech_bboxes = bboxes_per_frame[len(frames16):]
    logger.info(
        f"Detection done in {time.time()-t_det0:.1f}s "
        f"({sum(1 for b in main_bboxes if b)}/{len(main_bboxes)} main frames detected)"
    )

    # ── Consolidate character names across all frames ─────────────────────
    all_raw_names = [b.name for per_frame in main_bboxes for b in per_frame]
    name_mapping = DET.consolidate_character_names(all_raw_names, ctx=ctx)
    # Apply mapping
    canonical_bboxes_per_frame = [
        [DET.CharacterBbox(
            name=name_mapping.get(b.name, b.name),
            body=b.body,
            face=b.face,
        ) for b in per_frame]
        for per_frame in main_bboxes
    ]

    # Group bboxes by canonical name — but keep at most ONE bbox per character per frame
    # (pick the largest body bbox of that character in each frame, if it appears multiple times)
    from collections import defaultdict
    per_character_frames: dict[str, list] = defaultdict(list)  # name → [(frame_idx, ts, img, bbox)]
    for f_idx, ((ts, img), bboxes) in enumerate(zip(frames16, canonical_bboxes_per_frame)):
        by_name: dict[str, DET.CharacterBbox] = {}
        for b in bboxes:
            area = (b.body[2] - b.body[0]) * (b.body[3] - b.body[1])
            existing = by_name.get(b.name)
            if existing is None or area > (existing.body[2] - existing.body[0]) * (existing.body[3] - existing.body[1]):
                by_name[b.name] = b
        for name, b in by_name.items():
            per_character_frames[name].append((f_idx, ts, img, b))

    # Keep characters that appear in ≥ 4 frames (else too little data for a per-char grid)
    MIN_APPEARANCES = 4
    main_characters = [
        name for name, appearances in per_character_frames.items()
        if len(appearances) >= MIN_APPEARANCES
    ]
    # Sort by total appearances desc (most prominent first)
    main_characters.sort(key=lambda n: -len(per_character_frames[n]))
    logger.info(
        f"Characters detected: {list(per_character_frames.keys())} — "
        f"analysing {len(main_characters)}: {main_characters}"
    )

    # ── Stabilise frames (homography registration) for Environment Stability ─
    # The raw keyframe grid can't distinguish camera-reveal from object-teleport.
    # Registering every frame into frame 0's coordinate system (using feature
    # matching on the background only — character bboxes are masked out)
    # removes camera motion. In the stabilised view, background objects that
    # truly teleport stand out; things that were just revealed by a pan don't.
    _emit({"type": "stage", "stage": "stabilising"})
    t_stab0 = time.time()
    stabilised_frames, n_registered = STAB.stabilise_frames(frames16, canonical_bboxes_per_frame)
    logger.info(
        f"Stabilisation: {n_registered}/{len(frames16)-1} frames registered "
        f"(in {time.time()-t_stab0:.1f}s) — these remove camera motion for Environment Stability."
    )

    # ── Compose keyframe grid (STABILISED — fed to Environment Stability)
    # 4×6 = 24 panels at 440px.
    keyframe_grid_img = KF.compose_grid(stabilised_frames, cols=4, panel_width=440)
    keyframe_grid_img.save(run_dir / "keyframe_grid.png")
    keyframe_grid_bytes = KF.image_to_png_bytes(keyframe_grid_img)

    # Also save the raw (unstabilised) grid for debugging/visual inspection
    raw_grid_img = KF.compose_grid(frames16, cols=4, panel_width=440)
    raw_grid_img.save(run_dir / "keyframe_grid_raw.png")

    # ── Compose per-character body + face + eye grids ─────────────────────
    # Bumped to 24 panels (4×6 layout). Each character now gets THREE grids:
    #   - body_grid: full body, for Body Consistency
    #   - face_grid: whole face, for Face & Uncanny + Character Consistency Audit
    #   - eye_grid:  zoomed to just the eye region, for Facial Topology Audit
    #     (max pixel density for aspect-ratio / curvature measurement)
    PER_CHAR_PANELS = 24
    PER_CHAR_COLS = 4  # 4×6 layout
    per_character_grids: dict[str, dict[str, bytes]] = {}
    for cname in main_characters:
        appearances = per_character_frames[cname]
        if len(appearances) > PER_CHAR_PANELS:
            import numpy as np
            idxs = np.linspace(0, len(appearances) - 1, PER_CHAR_PANELS, dtype=int).tolist()
            sampled = [appearances[i] for i in idxs]
        else:
            sampled = appearances

        # --- body grid ---
        body_pairs = [
            (ts, DET.crop_with_padding(img, b.body, padding=0.15))
            for (_, ts, img, b) in sampled
        ]
        body_grid_img = KF.compose_grid(body_pairs, cols=PER_CHAR_COLS, panel_width=420)
        slug = cname.replace(" ", "_").replace("/", "_")
        body_grid_img.save(run_dir / f"body_grid__{slug}.png")

        # --- face grid ---
        face_pairs = []
        for (_, ts, img, b) in sampled:
            face_bbox = b.face or (
                b.body[0], b.body[1],
                b.body[2], b.body[1] + int((b.body[3] - b.body[1]) * 0.45)
            )
            face_pairs.append((ts, DET.crop_with_padding(
                img, face_bbox, padding=0.25,
                fallback_region=(0.25, 0.15, 0.75, 0.55),
            )))
        face_grid_img = KF.compose_grid(face_pairs, cols=PER_CHAR_COLS, panel_width=340)
        face_grid_img.save(run_dir / f"face_grid__{slug}.png")

        # --- eye grid (for Facial Topology Audit) ---
        # Zoom to the upper face region — gives the geometric audit 3-5× more
        # pixels on the actual eye opening than the whole-face crop.
        eye_pairs = []
        for (_, ts, img, b) in sampled:
            face_bbox_or_fallback = b.face or (
                b.body[0], b.body[1],
                b.body[2], b.body[1] + int((b.body[3] - b.body[1]) * 0.45)
            )
            iw, ih = img.size
            eye_bbox = DET.eye_region_from_face(face_bbox_or_fallback, iw, ih)
            eye_pairs.append((ts, DET.crop_with_padding(
                img, eye_bbox, padding=0.1,
                fallback_region=(0.25, 0.20, 0.75, 0.50),
            )))
        eye_grid_img = KF.compose_grid(eye_pairs, cols=PER_CHAR_COLS, panel_width=400)
        eye_grid_img.save(run_dir / f"eye_grid__{slug}.png")

        per_character_grids[cname] = {
            "body_grid": KF.image_to_png_bytes(body_grid_img),
            "face_grid": KF.image_to_png_bytes(face_grid_img),
            "eye_grid":  KF.image_to_png_bytes(eye_grid_img),
        }

    # ── Classical lip-sync check (replaces the old VLM agent that hallucinated) ─
    # Optical flow in the mouth region across each Whisper speech segment.
    # Runs on CPU, no API calls, deterministic.
    lipsync_result = None
    if segments:
        _emit({"type": "stage", "stage": "running_lipsync"})
        t_lf0 = time.time()
        try:
            lipsync_result = LSF.analyse(str(local_video), segments, n_samples_per_segment=8, ctx=ctx)
            logger.info(
                f"Classical lipsync done in {time.time()-t_lf0:.1f}s — "
                f"verdict={lipsync_result.overall_verdict}"
            )
        except Exception as exc:
            logger.error(f"Classical lipsync failed: {exc}")

    # Build a speech grid anyway, for the viewer (not consumed by any agent now)
    if speech_frames_flat:
        speech_pair_images = []
        for (ts, img), bboxes in zip(speech_frames_flat, speech_bboxes):
            face_bbox = DET.largest_face_bbox(bboxes)
            speech_pair_images.append((
                ts,
                DET.crop_with_padding(img, face_bbox, padding=0.3,
                                      fallback_region=(0.25, 0.15, 0.75, 0.55)),
            ))
        speech_grid_img = KF.compose_grid(speech_pair_images, cols=2, panel_width=640)
        speech_grid_img.save(run_dir / "speech_grid.png")

    _emit({"type": "stage", "stage": "running_agents"})

    # ── Dispatch scene-level grid-dependent agents ─────────────────────────
    scene_dep_kwargs_pool = {
        "keyframe_grid":   keyframe_grid_bytes,
    }
    for name, fn, needs in A.SCENE_AGENTS:
        if name in GRID_INDEPENDENT:
            continue
        kwargs = {k: scene_dep_kwargs_pool[k] for k in needs}
        _submit(name, fn, kwargs)

    # ── Dispatch per-character agents ─────────────────────────────────────
    # 3 agents × N characters — all run in parallel.
    for cname in main_characters:
        grids = per_character_grids[cname]
        for agent_id, fn, needs in A.PER_CHARACTER_AGENTS:
            kw = {}
            for k in needs:
                if k == "character_name":
                    kw[k] = cname
                elif k in grids:
                    kw[k] = grids[k]
            key = f"{agent_id}::{cname}"
            _submit(key, fn, kw)

    # If no characters detected, fall back to running each per-char agent on a
    # centre-crop fallback grid so the pipeline still produces a report.
    if not main_characters:
        logger.warning("No characters detected with ≥4 appearances — running per-character agents on a centre-crop fallback grid.")
        fallback_crops = [
            (ts, DET.crop_with_padding(img, None, padding=0.15))
            for (ts, img) in frames16
        ]
        fallback_grid = KF.image_to_png_bytes(
            KF.compose_grid(fallback_crops, cols=4, panel_width=480)
        )
        fallback_grids = {"body_grid": fallback_grid, "face_grid": fallback_grid, "eye_grid": fallback_grid}
        for agent_id, fn, needs in A.PER_CHARACTER_AGENTS:
            kw = {}
            for k in needs:
                if k == "character_name":
                    kw[k] = "unknown character"
                elif k in fallback_grids:
                    kw[k] = fallback_grids[k]
            key = f"{agent_id}::unknown character"
            _submit(key, fn, kw)

    # Collect all agent results
    for fut in cf.as_completed(agent_futures):
        r = fut.result()
        with reports_lock:
            reports.append(r)
        _emit({
            "type": "agent_done",
            "agent_id": r.agent_id,
            "title": r.title,
            "verdict": r.verdict,
            "summary": r.summary,
            "elapsed_s": r.elapsed_s,
        })
        logger.info(f"✓ {r.title} — {r.verdict} ({r.elapsed_s}s)")
    pool.shutdown(wait=True)
    logger.info(f"All agents finished (wall-clock from detection start: {time.time()-t_det0:.1f}s)")

    # Stable ordering for the report: scene-level agents first (in registry
    # order), then per-character agents grouped by character.
    scene_order = {name: i for i, (name, _, _) in enumerate(A.SCENE_AGENTS)}
    per_char_order = {name: i for i, (name, _, _) in enumerate(A.PER_CHARACTER_AGENTS)}

    def _sort_key(r: A.AgentReport):
        if "::" in r.agent_id:
            base, cname = r.agent_id.split("::", 1)
            return (1, main_characters.index(cname) if cname in main_characters else 99,
                    per_char_order.get(base, 99))
        return (0, scene_order.get(r.agent_id, 99), 0)

    reports.sort(key=_sort_key)

    # ── Inject classical optical-flow lipsync report ────────────────────────
    if lipsync_result is not None:
        reports.append(A.AgentReport(
            agent_id="lipsync_optical_flow",
            title="Lip-Sync (optical flow)",
            focus="Classical flow-based mouth motion measurement",
            verdict=lipsync_result.overall_verdict,
            summary=lipsync_result.summary,
            findings=lipsync_result.findings,
            elapsed_s=0.0,
        ))

    # ── Run Prompt-vs-Caption using the blind captioner's output ────────────
    # Same skip rule as Prompt Fidelity: no prompt → nothing to compare against.
    blind_caption_report = next(
        (r for r in reports if r.agent_id == "blind_captioner"), None
    )
    if blind_caption_report is not None and has_prompt:
        _emit({"type": "stage", "stage": "running_pvc"})
        _emit({"type": "agent_started", "agent_id": "prompt_vs_caption",
               "title": "Prompt vs Blind Caption"})
        logger.info("Running Prompt-vs-Caption comparator …")
        t_pvc = time.time()
        try:
            pvc_report = A.run_prompt_vs_caption(
                blind_caption=blind_caption_report.findings or blind_caption_report.summary,
                transcript=transcript,
                prompt_text=prompt_text,
                ctx=ctx,
            )
            logger.info(
                f"Prompt-vs-Caption done in {time.time()-t_pvc:.1f}s — "
                f"{pvc_report.verdict}"
            )
            reports.append(pvc_report)
            _emit({
                "type": "agent_done",
                "agent_id": pvc_report.agent_id,
                "title": pvc_report.title,
                "verdict": pvc_report.verdict,
                "summary": pvc_report.summary,
                "elapsed_s": pvc_report.elapsed_s,
            })
        except Exception as exc:
            logger.error(f"Prompt-vs-Caption failed: {exc}")

    # ── Aggregator ──────────────────────────────────────────────────────────
    _emit({"type": "stage", "stage": "aggregating"})
    logger.info("Running aggregator …")
    t0 = time.time()
    agg = A.run_aggregator(reports, prompt_text, ctx=ctx)
    logger.info(f"Aggregator done in {time.time()-t0:.1f}s — {agg['overall_verdict']} {agg['score']}")
    _emit({"type": "stage", "stage": "done"})

    # ── Optional Prompt Reasoner ────────────────────────────────────────────
    prompt_reasoner_output = None
    if enable_prompt_reasoning:
        logger.info("Running Prompt Reasoner (Deduction Engine) …")
        t0 = time.time()
        prompt_reasoner_output = A.run_prompt_reasoner(
            video_bytes, reports, agg, prompt_text, ctx=ctx,
        )
        n_edits = len(prompt_reasoner_output.get("counterfactual_edits", []))
        skipped = prompt_reasoner_output.get("skipped_reason")
        if skipped:
            logger.info(f"Prompt Reasoner skipped ({time.time()-t0:.1f}s) — {skipped}")
        else:
            logger.info(f"Prompt Reasoner done in {time.time()-t0:.1f}s — {n_edits} edit(s) proposed")

    # Persist detected bboxes
    detections_dump = [
        [{"name": b.name, "body": list(b.body), "face": list(b.face) if b.face else None}
         for b in per_frame]
        for per_frame in bboxes_per_frame
    ]

    payload = {
        "video_file": local_video.name,
        "prompt": prompt_text,
        "mode": ctx.mode.value,
        "transcript": transcript,
        "speech_captions": speech_captions,
        "timestamp": tag,
        "detections": detections_dump,
        "characters": [
            {
                "name": cname,
                "appearances": len(per_character_frames.get(cname, [])),
                "body_grid": f"body_grid__{cname.replace(' ', '_').replace('/', '_')}.png",
                "face_grid": f"face_grid__{cname.replace(' ', '_').replace('/', '_')}.png",
                "eye_grid":  f"eye_grid__{cname.replace(' ', '_').replace('/', '_')}.png",
            }
            for cname in main_characters
        ],
        "character_name_mapping": name_mapping,
        "agents": [r.to_dict() for r in reports],
        "aggregator": agg,
        "prompt_reasoner": prompt_reasoner_output,
        "cost": ctx.cost_tracker.to_dict(),
        "wall_clock_seconds": round(time.time() - t_total_start, 1),
    }
    (run_dir / "report.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    (run_dir / "report.md").write_text(_markdown_report(payload))
    logger.info(f"Total wall-clock: {payload['wall_clock_seconds']}s")
    return payload


def _markdown_report(p: dict) -> str:
    agg = p["aggregator"]
    cost = p.get("cost", {}) or {}
    cost_line = (
        f"  ·  **Cost:** ${cost.get('total_usd', 0.0):.4f} "
        f"({cost.get('calls', 0)} calls)"
        if cost else ""
    )
    out = [
        f"# {p['video_file']}",
        "",
        f"**Verdict:** {agg['overall_verdict']}  ·  **Score:** {agg['score']}/100  "
        f"·  **Mode:** `{p.get('mode', '?')}`  "
        f"·  **Wall-clock:** {p.get('wall_clock_seconds', '?')}s{cost_line}",
        "",
        "### Headline", agg["headline"], "",
        "### What works", agg["what_works"], "",
        "### What breaks", agg["what_breaks"], "",
        "### Top issue", agg["top_issue"], "",
        "---",
        "## Agent analyses", "",
    ]
    for r in p["agents"]:
        out += [
            f"### {r['title']}  _[{r['verdict']}]_",
            f"**{r['summary']}**",
            "",
            r["findings"],
            "",
        ]
    pr = p.get("prompt_reasoner")
    if pr:
        out += ["---", "## Prompt Reasoning (Deduction Engine)", ""]
        if pr.get("skipped_reason"):
            out += [f"_Skipped: {pr['skipped_reason']}_", ""]
        else:
            if pr.get("rationale"):
                out += ["### Rationale", pr["rationale"], ""]
            conflicts = pr.get("diagnosed_conflicts") or []
            if conflicts:
                out += ["### Diagnosed conflicts (intent vs outcome)", ""]
                for c in conflicts:
                    cap = " _(capacity-limited — prompt fix won't help)_" if c.get("is_capacity_limited") else ""
                    out += [
                        f"- **Intent:** {c.get('intent','')}",
                        f"  **Outcome:** {c.get('outcome','')} _(source: {c.get('source_agent','')})_{cap}",
                    ]
                out += [""]
            triggers = pr.get("trigger_attributions") or []
            if triggers:
                out += ["### Trigger attributions", ""]
                for t in triggers:
                    out += [
                        f"- **Visible element:** {t.get('visible_element','')}  "
                        f"(confidence: {t.get('confidence','')})",
                        f"  **Prompt phrase:** \"{t.get('prompt_phrase','')}\"",
                        f"  **Hypothesised bias:** {t.get('hypothesized_bias','')}",
                    ]
                out += [""]
            edits = pr.get("counterfactual_edits") or []
            if edits:
                out += ["### Counterfactual edits", ""]
                for e in edits:
                    out += [
                        f"- **{e.get('operation','').upper()}** \"{e.get('from','')}\" → \"{e.get('to','')}\"",
                        f"  _{e.get('reasoning','')}_",
                    ]
                out += [""]
            if pr.get("rewritten_prompt"):
                out += ["### Rewritten prompt", "```", pr["rewritten_prompt"], "```", ""]
    out += ["---", "### Transcript", "```", p["transcript"], "```"]
    return "\n".join(out)


def build_run_context(
    *,
    mode: Mode | str = DEFAULT_MODE,
    accept_cost: bool = False,
    max_spend_usd: float | None = None,
    enable_prompt_reasoning: bool = False,
) -> RunContext:
    """Library entry point: validate the safety layers and return a RunContext.

    Raises ExpensiveModeDisabled if `mode` is pro/max and any of the three
    layers blocks it. Cheap and mock modes never raise here.
    """
    if isinstance(mode, str):
        mode = Mode.parse(mode)
    require_mode_allowed(mode, accept_cost=accept_cost, max_spend_usd=max_spend_usd)
    return RunContext(
        mode=mode,
        cost_tracker=CostTracker(max_spend_usd=max_spend_usd),
        accept_cost=accept_cost,
        max_spend_usd=max_spend_usd,
        enable_prompt_reasoning=enable_prompt_reasoning,
    )


def main() -> None:
    args = parse_args()
    prompt_text = args.prompt
    if args.prompt_file:
        prompt_text = Path(args.prompt_file).read_text()

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    ctx = build_run_context(
        mode=args.mode,
        accept_cost=args.i_accept_cost,
        max_spend_usd=args.max_spend_usd,
        enable_prompt_reasoning=args.enable_prompt_reasoning,
    )

    print(f"\n{'=' * 60}")
    print(f"  VLM Video QA — {os.path.basename(args.video_path)}")
    print(f"  mode={ctx.mode.value}"
          f"{f'  max_spend=${ctx.max_spend_usd:.2f}' if ctx.max_spend_usd else ''}")
    print(f"{'=' * 60}\n")
    payload = run_video(args.video_path, prompt_text, out_dir, ctx=ctx)
    cost = payload.get("cost", {}) or {}
    print(f"\nCost: ${cost.get('total_usd', 0.0):.4f}  "
          f"({cost.get('calls', 0)} calls, mode={payload.get('mode')})")


if __name__ == "__main__":
    main()
