#!/usr/bin/env python3
"""Render BOTH galleries for an existing dataset:
  - source_gallery.mp4: raw clips letterboxed into the 960x544 canvas, full
    duration, so original framing is visible.
  - training_gallery.mp4: processed_videos/ (already letterboxed +
    49-frame-trimmed) — exactly what the trainer's latents were encoded
    from.

Side effect: if `processed_videos/` is missing, it's regenerated from
`videos/` via gallery.transform_to_training_format. That same directory is
what the trainer consumes; so what you watch in training_gallery.mp4 is
guaranteed to match what the model sees, byte-for-byte aligned.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "dataset_pipeline"))

from captions import apply_brand_tokens, load_brand_tokens  # noqa: E402
from gallery import (  # noqa: E402
    build_concat_list,
    check_ffmpeg,
    concat_gallery,
    render_per_clip,
    transform_to_training_format,
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_processed_videos(root: Path, clips: list[dict]) -> int:
    """Pre-process raw clips → processed_videos/{idx:04d}.mp4 (letterbox +
    49-frame trim). Idempotent: skip clips whose processed file exists."""
    src_dir = root / "videos"
    dst_dir = root / "processed_videos"
    dst_dir.mkdir(parents=True, exist_ok=True)
    new = 0
    for c in clips:
        if not c.get("download_ok", True) or not c.get("prompt"):
            continue
        idx = c["index"]
        src = src_dir / f"{idx:04d}.mp4"
        dst = dst_dir / f"{idx:04d}.mp4"
        if not src.exists():
            print(f"  [{idx:04d}] WARN raw clip missing: {src}", flush=True)
            continue
        if dst.exists() and dst.stat().st_size > 0:
            continue
        try:
            transform_to_training_format(src, dst)
            new += 1
            print(f"  [{idx:04d}] preprocessed → {dst.name}", flush=True)
        except subprocess.CalledProcessError as e:
            print(f"  [{idx:04d}] PREPROCESS FAIL: {e}", flush=True)
    return new


def render_gallery(root: Path, clips: list[dict], mode: str, out_path: Path,
                   token_map: dict[str, str]) -> int:
    """Render one gallery (source or training) by overlaying captions on
    each clip + concat with black fillers. Returns active rendered count."""
    work_dir = root / f"_qa_render_cache" / mode
    per_clip_dir = work_dir / "per_clip"
    per_clip_dir.mkdir(parents=True, exist_ok=True)
    src_dir = root / ("videos" if mode == "source" else "processed_videos")

    rendered = skipped = failed = 0
    for c in clips:
        if not c.get("download_ok", True) or not c.get("prompt"):
            continue
        idx = c["index"]
        src = src_dir / f"{idx:04d}.mp4"
        dst = per_clip_dir / f"{idx:04d}.mp4"
        if dst.exists() and dst.stat().st_size > 0:
            skipped += 1
            continue
        if not src.exists():
            failed += 1
            continue
        try:
            display_prompt = apply_brand_tokens(c["prompt"], token_map)
            render_per_clip(src, display_prompt, idx, dst, mode=mode)
            rendered += 1
        except subprocess.CalledProcessError as e:
            print(f"  [{mode}] [{idx:04d}] FAIL: {e}", flush=True)
            failed += 1

    active = [c["index"] for c in clips
              if c.get("download_ok", True) and c.get("prompt")
              and (per_clip_dir / f"{c['index']:04d}.mp4").exists()]
    concat_list = work_dir / "concat.txt"
    build_concat_list(active, per_clip_dir, concat_list)
    concat_gallery(concat_list, out_path)
    print(f"  [{mode}] rendered={rendered} skipped={skipped} failed={failed} → "
          f"{out_path.name} ({out_path.stat().st_size/1024:.0f} KB)", flush=True)
    return len(active)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True, help="Local dataset root dir")
    args = ap.parse_args()

    check_ffmpeg()
    root = Path(args.dataset).resolve()
    md_path = root / "metadata.json"
    if not md_path.exists():
        print(f"ERROR: {md_path} not found", file=sys.stderr)
        return 2

    md = json.loads(md_path.read_text())
    clips = md.get("clips", [])
    token_map = load_brand_tokens()

    print(f"=== {root.name}: {len(clips)} clip rows ===", flush=True)
    print(f"    brand-token substitutions (just-in-time): {token_map}", flush=True)

    print("\n[step 1/3] preprocess raw clips → processed_videos/ (letterbox + 49-frame trim)")
    new_processed = ensure_processed_videos(root, clips)
    print(f"  new this run: {new_processed}")

    print("\n[step 2/3] render source_gallery.mp4 (raw clips, full duration)")
    src_count = render_gallery(root, clips, mode="source",
                               out_path=root / "source_gallery.mp4",
                               token_map=token_map)

    print("\n[step 3/3] render training_gallery.mp4 (processed clips, 49 frames each)")
    trn_count = render_gallery(root, clips, mode="training",
                               out_path=root / "training_gallery.mp4",
                               token_map=token_map)

    md["galleries"] = {
        "source":   {"path": "source_gallery.mp4",   "rendered_count": src_count, "interclip_black_s": 0.5,
                     "transform": "letterbox to 960x544, full original duration, audio preserved"},
        "training": {"path": "training_gallery.mp4", "rendered_count": trn_count, "interclip_black_s": 0.5,
                     "transform": "letterbox to 960x544, trimmed to 49 frames @24fps (matches trainer view)"},
    }
    # Drop legacy single-gallery field if present.
    md.pop("qa_gallery", None)
    md.pop("gallery", None)
    md["rendered_at"] = utc_now()
    md_path.write_text(json.dumps(md, indent=2, ensure_ascii=False))
    print(f"\nUpdated {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
