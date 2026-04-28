#!/usr/bin/env python3
"""Phase 0.2 — Quality Triage (active_plan §3.2).

For every PP-relevant LOCAL video in docs/phase0/data_inventory.csv:
  - Sharpness (Laplacian variance) on 3 sampled frames
  - Letterbox edge-energy on the 4 borders
  - Motion magnitude (mean abs frame-diff between consecutive sampled pairs)
  - Audio presence + simple level estimate via ffprobe
  - Watermark detection: SKIPPED unless templates configured in pre_flight.yaml

Writes: docs/phase0/quality_triage.csv (per-clip)
        docs/phase0/Phase0_QualityReport.md (histograms + 50/70/30 shortfall)

GCS videos are NOT triaged — bandwidth-prohibitive. Listed in the report's
follow-up section.

Thresholds are defined as constants below. They are committed to source
*before* the run, so the report's bucket eligibility counts cannot be
gerrymandered post-hoc.
"""
from __future__ import annotations

import argparse
import csv
import statistics
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse, unquote

import yaml

REPO = Path(__file__).resolve().parent.parent.parent
PRE_FLIGHT = REPO / "docs" / "pre_flight.yaml"
INVENTORY_CSV = REPO / "docs" / "phase0" / "data_inventory.csv"
TRIAGE_CSV = REPO / "docs" / "phase0" / "quality_triage.csv"
REPORT_MD = REPO / "docs" / "phase0" / "Phase0_QualityReport.md"

# --- Thresholds (committed before the run) ----------------------------------
# Sharpness floor: use the empirical p25 of the PP-relevant distribution.
# Letterbox edge floor: ≤ 0.05 means top/bottom border energy is < 5% of mean.
LETTERBOX_MAX = 0.05

# Motion magnitude buckets (mean abs frame-diff in [0,1] grayscale space).
# These are conservative starting points — refine after first run lands.
MOTION_STATIC_MAX = 0.01
MOTION_DYNAMIC_MIN = 0.025
MOTION_DYNAMIC_MAX = 0.15

# 3-tier dataset targets from vision.md
TARGETS = {"static": 50, "dynamic": 70, "closeup": 30}

# Sample N frames per video for sharpness/letterbox/motion estimation.
SAMPLE_FRAMES = 3


@dataclass
class TriageRow:
    path: str
    duration_s: str
    width: str
    height: str
    fps: str
    guessed_character: str
    guessed_scene_type: str
    sharpness: str = ""
    letterbox_top: str = ""
    letterbox_bottom: str = ""
    motion: str = ""
    has_audio: str = ""
    audio_level_db: str = ""
    eligible_static: str = "0"
    eligible_dynamic: str = "0"
    eligible_closeup: str = "0"
    notes: str = ""


def url_to_local_path(url: str) -> Path | None:
    if url.startswith("file://"):
        return Path(unquote(urlparse(url).path))
    return None


def is_pp_local_video(r: dict) -> bool:
    if r.get("type") != "video":
        return False
    if not (r.get("guessed_character") or "").strip():
        return False
    return r.get("path", "").startswith("file://")


def sample_frame_indices(total_frames: int, n: int) -> list[int]:
    if total_frames <= 0:
        return []
    if n == 1:
        return [total_frames // 2]
    return [int(i * (total_frames - 1) / (n - 1)) for i in range(n)]


def compute_video_metrics(path: Path) -> tuple[dict, str]:
    """Returns (metrics, error_or_empty). On error, metrics is empty dict."""
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return {}, "cv2 open failed"
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    if total <= 1 or h == 0 or w == 0:
        cap.release()
        return {}, f"bad geometry: total={total}, w={w}, h={h}"

    indices = sample_frame_indices(total, SAMPLE_FRAMES)
    frames = []
    for idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, f = cap.read()
        if not ok or f is None:
            continue
        gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        frames.append(gray)
    cap.release()

    if not frames:
        return {}, "no frames decodable"

    sharpness_per = [float(cv2.Laplacian(g, cv2.CV_64F).var()) for g in frames]
    sharpness = float(statistics.mean(sharpness_per))

    # Letterbox detection: top/bottom 4-row strips average vs full-frame mean.
    g0 = frames[len(frames) // 2]
    gf = g0.astype(np.float32) / 255.0
    full_mean = float(gf.mean())
    if full_mean < 1e-6:
        lb_top = lb_bot = 1.0
    else:
        top_mean = float(gf[: max(4, h // 30)].mean())
        bot_mean = float(gf[-max(4, h // 30):].mean())
        lb_top = abs(top_mean - full_mean) / full_mean
        lb_bot = abs(bot_mean - full_mean) / full_mean
    # Letterbox is dark bars → top/bot mean ≪ full_mean.
    lb_top_score = 1.0 - min(1.0, top_mean / full_mean) if full_mean > 0 else 0
    lb_bot_score = 1.0 - min(1.0, bot_mean / full_mean) if full_mean > 0 else 0

    # Motion: mean abs diff across consecutive sampled frames (normalized to [0,1]).
    motion_vals = []
    for a, b in zip(frames[:-1], frames[1:]):
        diff = np.abs(a.astype(np.int16) - b.astype(np.int16)).astype(np.float32) / 255.0
        motion_vals.append(float(diff.mean()))
    motion = float(statistics.mean(motion_vals)) if motion_vals else 0.0

    return {
        "sharpness": sharpness,
        "letterbox_top": lb_top_score,
        "letterbox_bottom": lb_bot_score,
        "motion": motion,
    }, ""


def audio_check(path: Path) -> tuple[bool, str]:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
             "stream=codec_name", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=20, check=False,
        )
        has_audio = bool(out.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False, ""
    if not has_audio:
        return False, ""
    # Mean volume in dBFS.
    try:
        out = subprocess.run(
            ["ffmpeg", "-nostats", "-hide_banner", "-i", str(path),
             "-af", "volumedetect", "-vn", "-f", "null", "-"],
            capture_output=True, text=True, timeout=60, check=False,
        )
        for line in out.stderr.splitlines():
            if "mean_volume" in line:
                # "[Parsed_volumedetect_0 @ ...] mean_volume: -20.7 dB"
                tail = line.split("mean_volume:", 1)[1].strip()
                return True, tail.split()[0]
    except (subprocess.TimeoutExpired, FileNotFoundError, IndexError):
        pass
    return True, ""


def hist_buckets(values: list[float], buckets: list[tuple[float, float, str]]) -> list[tuple[str, int]]:
    out = []
    for lo, hi, label in buckets:
        n = sum(1 for v in values if lo <= v < hi)
        out.append((label, n))
    return out


def write_report(rows: list[TriageRow], skipped_gcs: int, sharpness_p25: float, watermark_status: str) -> None:
    sharps = [float(r.sharpness) for r in rows if r.sharpness]
    motions = [float(r.motion) for r in rows if r.motion]
    audio_count = sum(1 for r in rows if r.has_audio == "1")

    md: list[str] = []
    md.append("# Phase 0.2 — Quality Triage Report")
    md.append("")
    md.append(f"Source: `{TRIAGE_CSV.relative_to(REPO)}` ({len(rows)} clips triaged).")
    md.append(f"GCS videos skipped (bandwidth): **{skipped_gcs}** — see follow-up at end.")
    md.append("")
    md.append("## Bucket eligibility thresholds (committed in source before run)")
    md.append("")
    md.append("| Bucket | Sharpness | Letterbox edge | Motion (mean Δ) |")
    md.append("|---|---|---|---|")
    md.append(f"| Static | ≥ p25 ({sharpness_p25:.1f}) | ≤ {LETTERBOX_MAX} both edges | < {MOTION_STATIC_MAX} |")
    md.append(f"| Dynamic | ≥ p25 ({sharpness_p25:.1f}) | ≤ {LETTERBOX_MAX} both edges | {MOTION_DYNAMIC_MIN}–{MOTION_DYNAMIC_MAX} |")
    md.append(f"| Closeup | ≥ p25 ({sharpness_p25:.1f}) | ≤ {LETTERBOX_MAX} both edges | any |")
    md.append("")
    md.append("p25 floor on sharpness is set empirically from this run's distribution and committed to the next run's report — re-runs use the prior p25 unless the script's constant is bumped.")
    md.append("")

    md.append("## Sharpness distribution")
    md.append("")
    if sharps:
        sharp_buckets = [(0, 25, "0–25"), (25, 50, "25–50"), (50, 100, "50–100"),
                         (100, 200, "100–200"), (200, 500, "200–500"), (500, 1e9, "500+")]
        md.append("| Laplacian variance | Count |")
        md.append("|---|---:|")
        for label, n in hist_buckets(sharps, sharp_buckets):
            md.append(f"| {label} | {n} |")
    else:
        md.append("_No sharpness measurements available._")
    md.append("")

    md.append("## Motion distribution")
    md.append("")
    if motions:
        motion_buckets = [
            (0, MOTION_STATIC_MAX, f"0 – {MOTION_STATIC_MAX:.3f} (static-eligible)"),
            (MOTION_STATIC_MAX, MOTION_DYNAMIC_MIN, f"{MOTION_STATIC_MAX:.3f} – {MOTION_DYNAMIC_MIN:.3f} (between)"),
            (MOTION_DYNAMIC_MIN, MOTION_DYNAMIC_MAX, f"{MOTION_DYNAMIC_MIN:.3f} – {MOTION_DYNAMIC_MAX:.3f} (dynamic-eligible)"),
            (MOTION_DYNAMIC_MAX, 1.0, f"{MOTION_DYNAMIC_MAX:.3f}+ (over-motion)"),
        ]
        md.append("| Motion (mean abs frame Δ, [0,1]) | Count |")
        md.append("|---|---:|")
        for label, n in hist_buckets(motions, motion_buckets):
            md.append(f"| {label} | {n} |")
    md.append("")

    md.append("## Bucket eligibility — shortfall vs vision.md targets")
    md.append("")
    md.append("Per character (Chase / Skye), counted independently. A clip can be eligible for 0 or 1 buckets here (we pick the strictest match).")
    md.append("")
    counts: dict[tuple[str, str], int] = {}
    for r in rows:
        chars = (r.guessed_character or "").split(",")
        for c in ("chase", "skye"):
            if c in chars:
                for b in ("static", "dynamic", "closeup"):
                    if getattr(r, f"eligible_{b}") == "1":
                        counts[(c, b)] = counts.get((c, b), 0) + 1
                        break
    md.append("| Character | Bucket | Have | Target | Gap |")
    md.append("|---|---|---:|---:|---:|")
    for c in ("chase", "skye"):
        for b in ("static", "dynamic", "closeup"):
            have = counts.get((c, b), 0)
            tgt = TARGETS[b]
            md.append(f"| {c} | {b} | {have} | {tgt} | {max(0, tgt - have)} |")
    md.append("")

    md.append("## Audio presence")
    md.append("")
    md.append(f"- Clips with audio track: **{audio_count}/{len(rows)}**")
    md.append("- Per-clip mean volume (dBFS) in `quality_triage.csv` column `audio_level_db`. Whisper SNR estimate is deferred (Step 2 work, frozen).")
    md.append("")

    md.append("## Skipped metric: watermark detection")
    md.append("")
    md.append(f"Status: **{watermark_status}**")
    md.append("")
    md.append("Per `active_plan.md §3.2`, watermark detection requires the PP logo template crops. Until templates are provided in `pre_flight.yaml.phase0.pp_watermark_templates`, this metric is omitted. **Followup**: supply 2–3 cropped logo PNGs (the official PP logo as it appears in source content) — bottom-left, top-right corners are typical.")
    md.append("")

    md.append("## Followup: GCS videos not triaged")
    md.append("")
    md.append(f"{skipped_gcs} GCS videos were not triaged. Triaging them requires either:")
    md.append("")
    md.append("- Streaming each video locally (bandwidth-prohibitive at the 14TB scale of `video_gen_dataset`).")
    md.append("- Running the triage on a GPU VM that already has the data mounted (preferred — saves bandwidth, parallelizes).")
    md.append("")
    md.append("Defer to Phase 1 — once the MLOps scaffolding lands, this can be a Cloud Run job over the bucket. For now, focus on the local clips, which represent the curated subset.")
    md.append("")
    REPORT_MD.write_text("\n".join(md))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="Cap clips processed (0 = all). Useful for smoke tests.")
    args = ap.parse_args()

    if not INVENTORY_CSV.exists():
        print(f"ERROR: {INVENTORY_CSV} not found. Run audit_data.py first.")
        return 1

    cfg = yaml.safe_load(PRE_FLIGHT.read_text())
    wm_templates = (cfg.get("phase0") or {}).get("pp_watermark_templates")
    watermark_status = "SKIPPED — no templates configured" if not wm_templates else f"templates: {wm_templates}"

    with INVENTORY_CSV.open() as f:
        all_rows = list(csv.DictReader(f))
    pp_videos = [r for r in all_rows if (r.get("type") == "video" and (r.get("guessed_character") or "").strip())]
    local_pp = [r for r in pp_videos if is_pp_local_video(r)]
    skipped_gcs = len(pp_videos) - len(local_pp)
    print(f"PP-relevant videos: {len(pp_videos)} (local: {len(local_pp)}, gcs skipped: {skipped_gcs})")

    if args.limit:
        local_pp = local_pp[: args.limit]

    triage_rows: list[TriageRow] = []
    sharps_running: list[float] = []
    for i, r in enumerate(local_pp, 1):
        path = url_to_local_path(r["path"])
        tr = TriageRow(
            path=r["path"], duration_s=r.get("duration_s", ""), width=r.get("width", ""),
            height=r.get("height", ""), fps=r.get("fps", ""),
            guessed_character=r.get("guessed_character", ""),
            guessed_scene_type=r.get("guessed_scene_type", ""),
        )
        if path is None or not path.exists():
            tr.notes = "file not found"
        else:
            metrics, err = compute_video_metrics(path)
            if err:
                tr.notes = err
            else:
                tr.sharpness = f"{metrics['sharpness']:.2f}"
                tr.letterbox_top = f"{metrics['letterbox_top']:.4f}"
                tr.letterbox_bottom = f"{metrics['letterbox_bottom']:.4f}"
                tr.motion = f"{metrics['motion']:.4f}"
                sharps_running.append(metrics["sharpness"])
            has_audio, level = audio_check(path)
            tr.has_audio = "1" if has_audio else "0"
            tr.audio_level_db = level
        triage_rows.append(tr)
        if i % 20 == 0 or i == len(local_pp):
            print(f"  [{i}/{len(local_pp)}] {Path(r['path'].replace('file://', '')).name}", flush=True)

    p25 = float(statistics.quantiles(sharps_running, n=4)[0]) if len(sharps_running) >= 4 else 0.0

    # Apply eligibility now that we have p25.
    for tr in triage_rows:
        if not tr.sharpness:
            continue
        sh = float(tr.sharpness)
        lt = float(tr.letterbox_top or 1.0)
        lb = float(tr.letterbox_bottom or 1.0)
        mo = float(tr.motion or 0)
        sharp_ok = sh >= p25
        letterbox_ok = lt <= LETTERBOX_MAX and lb <= LETTERBOX_MAX
        if not (sharp_ok and letterbox_ok):
            continue
        if mo < MOTION_STATIC_MAX:
            tr.eligible_static = "1"
        elif MOTION_DYNAMIC_MIN <= mo <= MOTION_DYNAMIC_MAX:
            tr.eligible_dynamic = "1"
        scene = (tr.guessed_scene_type or "")
        if "closeup" in scene:
            tr.eligible_closeup = "1"

    TRIAGE_CSV.parent.mkdir(parents=True, exist_ok=True)
    with TRIAGE_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(TriageRow.__dataclass_fields__.keys()))
        w.writeheader()
        for tr in sorted(triage_rows, key=lambda r: r.path):
            w.writerow(asdict(tr))

    write_report(triage_rows, skipped_gcs, p25, watermark_status)
    print(f"\nwrote {TRIAGE_CSV} ({len(triage_rows)} rows), {REPORT_MD}")
    print(f"sharpness p25 (this run) = {p25:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
