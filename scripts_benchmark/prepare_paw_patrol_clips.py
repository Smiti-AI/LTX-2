#!/usr/bin/env python3
"""Prepare 5-second candidate clips from Paw Patrol episodes for human review.

Detects scene cuts with ffmpeg, keeps scenes >= 5s (skipping intro/outro
windows), extracts a centered 5s window per scene, and writes them to
output/paw_patrol_review/candidates/.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
from pathlib import Path

ROOT = Path("/Users/efrattaig/projects/sm/LTX-2/output/paw_patrol_review")
EPISODES_DIR = ROOT / "episodes"
CANDIDATES_DIR = ROOT / "candidates"
META_PATH = ROOT / "candidates_meta.json"

CLIP_SECONDS = 5.0
SCENE_THRESHOLD = 0.30
SKIP_HEAD = 60.0   # seconds of intro to skip
SKIP_TAIL = 60.0   # seconds of credits/outro to skip
TARGET_TOTAL = 30  # oversample beyond 20 so the user has room to reject


def probe_duration(path: Path) -> float:
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(path),
    ])
    return float(out.strip())


def detect_cuts(path: Path, threshold: float = SCENE_THRESHOLD) -> list[float]:
    """Return scene-cut timestamps (seconds) using ffmpeg's scene filter."""
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
        "-filter:v", f"select='gt(scene,{threshold})',showinfo",
        "-an", "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    cuts: list[float] = []
    for m in re.finditer(r"pts_time:([\d.]+)", proc.stderr):
        cuts.append(float(m.group(1)))
    return cuts


def extract_clip(src: Path, start: float, dst: Path) -> None:
    # -ss before -i for fast seek; re-encode for accurate cuts and consistent playback in browser.
    cmd = [
        "ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", str(src),
        "-t", f"{CLIP_SECONDS:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-loglevel", "error",
        str(dst),
    ]
    subprocess.run(cmd, check=True)


def scene_segments(cuts: list[float], duration: float) -> list[tuple[float, float]]:
    boundaries = [0.0, *cuts, duration]
    return [(boundaries[i], boundaries[i + 1]) for i in range(len(boundaries) - 1)]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--episodes", nargs="*", default=None,
                        help="Episode filenames to process (default: all in episodes/ not yet seen).")
    parser.add_argument("--target", type=int, default=TARGET_TOTAL,
                        help="Number of new candidates to extract.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--skip-head", type=float, default=SKIP_HEAD,
                        help="Seconds of intro to skip (default 60).")
    parser.add_argument("--skip-tail", type=float, default=SKIP_TAIL,
                        help="Seconds of credits/outro to skip (default 60).")
    parser.add_argument("--scene-threshold", type=float, default=SCENE_THRESHOLD,
                        help="ffmpeg scene-change threshold (default 0.30).")
    parser.add_argument("--min-scene-seconds", type=float, default=CLIP_SECONDS,
                        help="Min scene duration to qualify (default 5.0). When a scene is "
                             "shorter than CLIP_SECONDS, the 5s clip will span the next cut(s).")
    parser.add_argument("--start", type=float, default=None,
                        help="Only consider scenes starting at or after this time (seconds).")
    parser.add_argument("--end", type=float, default=None,
                        help="Only consider scenes ending at or before this time (seconds).")
    args = parser.parse_args()

    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)

    existing_meta: list[dict] = []
    if META_PATH.exists():
        existing_meta = json.loads(META_PATH.read_text())
    seen_episodes = {m["episode"] for m in existing_meta}
    next_id = max((m["id"] for m in existing_meta), default=0) + 1

    if args.episodes:
        episodes = [EPISODES_DIR / name for name in args.episodes]
        for ep in episodes:
            if not ep.exists():
                raise SystemExit(f"Episode not found: {ep}")
    else:
        episodes = [p for p in sorted(EPISODES_DIR.glob("*.mp4")) if p.name not in seen_episodes]
    if not episodes:
        raise SystemExit("No new episodes to process.")

    print(f"Processing {len(episodes)} episode(s); target={args.target} new candidates.")

    # Build a set of (episode_name, clip_start_int) already extracted, so we can
    # re-mine an episode for new candidates without picking the same windows.
    existing_clips: set[tuple[str, int]] = {
        (m["episode"], int(m["clip_start"])) for m in existing_meta
    }

    rng = random.Random(args.seed)
    per_episode_pool: list[tuple[Path, float, float]] = []  # (episode, start, end)

    for ep in episodes:
        duration = probe_duration(ep)
        print(f"[{ep.name}] duration={duration:.1f}s — detecting cuts (thr={args.scene_threshold})...")
        cuts = detect_cuts(ep, threshold=args.scene_threshold)
        print(f"[{ep.name}]   {len(cuts)} cuts")
        segments = scene_segments(cuts, duration)
        kept: list[tuple[float, float]] = []
        skipped = 0
        for s, e in segments:
            if (e - s) < args.min_scene_seconds:
                continue
            if s < args.skip_head or e > (duration - args.skip_tail):
                continue
            if args.start is not None and s < args.start:
                continue
            if args.end is not None and e > args.end:
                continue
            # Center a 5s window inside the scene; clamp to remain inside the trim window.
            clip_start = s + max(0.0, (e - s - CLIP_SECONDS) / 2.0)
            if clip_start + CLIP_SECONDS > duration - args.skip_tail:
                clip_start = max(args.skip_head, duration - args.skip_tail - CLIP_SECONDS)
            if (ep.name, int(clip_start)) in existing_clips:
                skipped += 1
                continue
            kept.append((s, e))
        print(f"[{ep.name}]   {len(kept)} new scenes >= {args.min_scene_seconds}s "
              f"(skipped {skipped} already-extracted)")
        for s, e in kept:
            per_episode_pool.append((ep, s, e))

    # Sample evenly across episodes.
    by_ep: dict[str, list[tuple[Path, float, float]]] = {}
    for item in per_episode_pool:
        by_ep.setdefault(item[0].name, []).append(item)
    for v in by_ep.values():
        rng.shuffle(v)

    chosen: list[tuple[Path, float, float]] = []
    while len(chosen) < args.target and any(by_ep.values()):
        for name, lst in by_ep.items():
            if not lst:
                continue
            chosen.append(lst.pop())
            if len(chosen) >= args.target:
                break

    # Per-episode duration cache so we can clamp tail in the extraction loop.
    durations = {ep.name: probe_duration(ep) for ep in episodes}

    print(f"\nSelected {len(chosen)} candidate clips. Extracting...")
    meta: list[dict] = list(existing_meta)
    for offset, (ep, s, e) in enumerate(chosen):
        i = next_id + offset
        # Center a 5s window inside the scene; clamp to the same trim window as the gate.
        clip_start = s + max(0.0, (e - s - CLIP_SECONDS) / 2.0)
        tail_limit = durations[ep.name] - args.skip_tail - CLIP_SECONDS
        if clip_start > tail_limit:
            clip_start = max(args.skip_head, tail_limit)
        stem = f"{ep.stem}__t{int(clip_start):05d}"
        dst = CANDIDATES_DIR / f"{stem}.mp4"
        if not dst.exists():
            extract_clip(ep, clip_start, dst)
        meta.append({
            "id": i,
            "filename": dst.name,
            "episode": ep.name,
            "scene_start": round(s, 3),
            "scene_end": round(e, 3),
            "clip_start": round(clip_start, 3),
        })
        print(f"  [{i:02d}] {dst.name}")

    META_PATH.write_text(json.dumps(meta, indent=2))
    print(f"\nWrote {META_PATH}")


if __name__ == "__main__":
    main()
