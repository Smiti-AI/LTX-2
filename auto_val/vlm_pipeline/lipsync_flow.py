"""
Lip-Sync check via CLASSICAL optical flow on the mouth region.

Replaces the old VLM-based lip-sync agent which hallucinated ~50% of the time.
This one is deterministic and evidence-based: for each Whisper speech segment
we compute dense optical flow in the detected mouth region across the
segment's frames. High flow during speech → the mouth is moving. Near-zero
flow during speech → the mouth is static, audio is "from nowhere".

Returns a structured result the orchestrator wraps into an AgentReport.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

import cv2
import numpy as np
from loguru import logger

from .core import RunContext
from .detection import CharacterBbox, detect_batch, largest_face_bbox


# Thresholds chosen from empirical inspection. Cartoon TTS lip-sync when
# actually animated shows flow magnitudes in the 1.5-4.0 px/frame range in
# the mouth subregion. Static (non-animated) mouths sit below 0.3.
_FLOW_MIN_SPEAKING = 0.6    # below this = no meaningful mouth motion
_FLOW_MAYBE_SUBTLE = 1.2    # between _MIN and here = motion but weak
_MIN_FRAMES_PER_SEG = 4     # need at least this many pairs for a valid segment


@dataclass
class SegmentFlow:
    text: str
    t_start: float
    t_end: float
    mean_flow: float        # px / frame (mouth region only)
    n_pairs: int            # number of frame-pairs used
    mouth_found: bool       # did we locate a mouth bbox in enough frames?


@dataclass
class LipSyncFlowResult:
    segments: List[SegmentFlow]
    overall_verdict: str    # "great" | "minor_issues" | "major_issues"
    summary: str
    findings: str


def _mouth_from_face(face_bbox: Tuple[int, int, int, int]) -> Tuple[int, int, int, int]:
    """Approximate mouth region inside a face bbox: lower 45% × middle 80%."""
    x1, y1, x2, y2 = face_bbox
    w, h = x2 - x1, y2 - y1
    mx1 = int(x1 + 0.10 * w)
    mx2 = int(x2 - 0.10 * w)
    my1 = int(y1 + 0.55 * h)
    my2 = int(y2)
    return (mx1, my1, mx2, my2)


def _flow_magnitude_in_box(
    prev_gray: np.ndarray,
    next_gray: np.ndarray,
    box: Tuple[int, int, int, int],
) -> float:
    """Mean magnitude of dense Farneback flow inside the box, in px."""
    flow = cv2.calcOpticalFlowFarneback(
        prev_gray, next_gray, None,
        pyr_scale=0.5, levels=3, winsize=15,
        iterations=3, poly_n=5, poly_sigma=1.2, flags=0,
    )
    x1, y1, x2, y2 = box
    x1 = max(0, x1); y1 = max(0, y1)
    x2 = min(flow.shape[1], x2); y2 = min(flow.shape[0], y2)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    sub = flow[y1:y2, x1:x2]
    mag = np.sqrt(sub[..., 0] ** 2 + sub[..., 1] ** 2)
    return float(mag.mean())


def analyse(
    video_path: str,
    segments,               # list of SpeechSegment from perception.py
    n_samples_per_segment: int = 8,
    *,
    ctx: RunContext,
) -> LipSyncFlowResult:
    """
    For each speech segment, sample N frames evenly, detect the face (via
    Gemini Flash on the first sampled frame of the segment for speed),
    then compute optical flow in the mouth region between consecutive pairs.
    """
    if not segments:
        return LipSyncFlowResult(
            segments=[],
            overall_verdict="great",
            summary="No speech in the clip — lip-sync check not applicable.",
            findings="",
        )

    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total <= 0:
        cap.release()
        return LipSyncFlowResult([], "great", "Could not read video.", "")

    # Gather ONE representative frame per segment for face detection
    from PIL import Image
    rep_frames: List[Tuple[float, Image.Image]] = []
    per_seg_samples: List[List[Tuple[float, np.ndarray]]] = []
    for s in segments:
        t_center = (s.start + s.end) / 2.0
        # Sample N frames evenly across the segment
        span = max(0.001, s.end - s.start)
        t_samples = np.linspace(s.start + 0.05 * span, s.end - 0.05 * span, n_samples_per_segment)
        samples = []
        for t in t_samples:
            frame_idx = int(max(0, min(total - 1, t * fps)))
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ok, frame = cap.read()
            if ok:
                samples.append((float(t), frame))
        per_seg_samples.append(samples)
        # Representative frame = middle sample, as PIL
        if samples:
            mid = samples[len(samples) // 2]
            rgb = cv2.cvtColor(mid[1], cv2.COLOR_BGR2RGB)
            rep_frames.append((mid[0], Image.fromarray(rgb)))
        else:
            rep_frames.append((t_center, Image.new("RGB", (640, 360))))
    cap.release()

    # Run Flash face detection on the representative frames in parallel
    logger.info(f"Lip-sync flow: detecting faces in {len(rep_frames)} representative frames …")
    rep_bboxes: List[List[CharacterBbox]] = detect_batch(rep_frames, max_workers=8, ctx=ctx)

    # For each segment, build mouth bbox from largest face → compute mean flow
    seg_results: List[SegmentFlow] = []
    for s, samples, bboxes in zip(segments, per_seg_samples, rep_bboxes):
        if len(samples) < _MIN_FRAMES_PER_SEG:
            seg_results.append(SegmentFlow(
                text=s.text.strip(), t_start=s.start, t_end=s.end,
                mean_flow=0.0, n_pairs=0, mouth_found=False,
            ))
            continue
        face_bbox = largest_face_bbox(bboxes)
        if face_bbox is None:
            # No face detected in representative frame — can't evaluate reliably
            seg_results.append(SegmentFlow(
                text=s.text.strip(), t_start=s.start, t_end=s.end,
                mean_flow=0.0, n_pairs=0, mouth_found=False,
            ))
            continue
        mouth_bbox = _mouth_from_face(face_bbox)

        # Compute frame-to-frame flow in the mouth region
        mags = []
        for i in range(len(samples) - 1):
            gray_a = cv2.cvtColor(samples[i][1], cv2.COLOR_BGR2GRAY)
            gray_b = cv2.cvtColor(samples[i + 1][1], cv2.COLOR_BGR2GRAY)
            mags.append(_flow_magnitude_in_box(gray_a, gray_b, mouth_bbox))
        mean_flow = float(np.mean(mags)) if mags else 0.0
        seg_results.append(SegmentFlow(
            text=s.text.strip(),
            t_start=s.start,
            t_end=s.end,
            mean_flow=mean_flow,
            n_pairs=len(mags),
            mouth_found=True,
        ))

    # Aggregate verdict
    valid = [r for r in seg_results if r.mouth_found and r.n_pairs > 0]
    if not valid:
        return LipSyncFlowResult(
            segments=seg_results,
            overall_verdict="great",
            summary="No face detected during speech — cannot reliably judge lip-sync.",
            findings="The speaker's face was not visible or detectable in any of the sampled speech frames; the check was skipped.",
        )

    static_mouths = [r for r in valid if r.mean_flow < _FLOW_MIN_SPEAKING]
    weak_mouths = [r for r in valid if _FLOW_MIN_SPEAKING <= r.mean_flow < _FLOW_MAYBE_SUBTLE]

    if len(static_mouths) >= max(2, len(valid) // 2):
        verdict = "major_issues"
        summary = (
            f"{len(static_mouths)} of {len(valid)} speech segments show "
            f"NO detectable mouth motion while audio plays — audio-from-nowhere."
        )
    elif len(static_mouths) >= 1:
        verdict = "minor_issues"
        summary = (
            f"{len(static_mouths)} of {len(valid)} speech segments show "
            f"no detectable mouth motion; others animate normally."
        )
    elif len(weak_mouths) >= len(valid) // 2:
        verdict = "minor_issues"
        summary = (
            f"Mouth motion is weak across {len(weak_mouths)} of {len(valid)} "
            f"segments — subtle lip-sync drift."
        )
    else:
        verdict = "great"
        summary = (
            f"All {len(valid)} speech segments show clear mouth animation "
            f"(mean flow > {_FLOW_MIN_SPEAKING} px/frame)."
        )

    # Concrete findings — one line per segment
    findings_lines = [
        f"Threshold: mouth-region flow < {_FLOW_MIN_SPEAKING:.2f} px/frame = static.",
        "",
    ]
    for r in seg_results:
        if not r.mouth_found:
            findings_lines.append(
                f"  Segment [{r.t_start:.2f}s–{r.t_end:.2f}s] \"{r.text[:60]}\": "
                f"face not detected, skipped."
            )
            continue
        label = (
            "STATIC"    if r.mean_flow < _FLOW_MIN_SPEAKING
            else "WEAK" if r.mean_flow < _FLOW_MAYBE_SUBTLE
            else "MOVING"
        )
        findings_lines.append(
            f"  Segment [{r.t_start:.2f}s–{r.t_end:.2f}s] \"{r.text[:60]}\": "
            f"mouth flow = {r.mean_flow:.2f} px/frame ({label})"
        )

    return LipSyncFlowResult(
        segments=seg_results,
        overall_verdict=verdict,
        summary=summary,
        findings="\n".join(findings_lines),
    )
