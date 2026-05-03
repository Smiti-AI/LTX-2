#!/usr/bin/env python3
"""
run_bm_sweep.py

Phase 3 inference sweep: each merged LoRA × each benchmark scene → one mp4.

Reads a directory of scenes (each containing config.json + start_frame.png in
the skye_greetings_v1 layout) and a directory of merged LoRA `.safetensors`
files (output of merge_skye_loras.py), and runs `inference.py` for every
(scene, adapter) pair.

Output layout:
    <out-dir>/<scene_name>/<adapter_basename>.mp4
    <out-dir>/blind_comparison/index.html

Idempotent: skips (scene, adapter) pairs whose mp4 already exists. Safe to
re-run after a crash or to add a new weight pair without re-doing finished
clips.

Usage
-----
    uv run python scripts/run_bm_sweep.py \\
        --bench-dir ~/inputs/benchmarks/skye_greetings_v1 \\
        --adapter-dir ~/skye_combined_v1/adapters \\
        --base-checkpoint ~/models/LTX-2.3/ltx-2.3-22b-dev.safetensors \\
        --text-encoder-path ~/models/gemma-3-12b-it-qat-q4_0-unquantized \\
        --out-dir ~/skye_combined_v1
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from html import escape
from pathlib import Path

DEFAULT_FPS = 24.0
DEFAULT_INFERENCE_STEPS = 30
DEFAULT_GUIDANCE = 4.0
DEFAULT_STG = 1.0


def _frames_for_duration(duration_s: float, fps: float) -> int:
    """Round (duration*fps + 1) down to the nearest k*8+1, the trainer's frame
    bucket constraint."""
    raw = int(round(duration_s * fps))
    # Need frames % 8 == 1
    frames = ((raw - 1) // 8) * 8 + 1
    if frames < 9:
        frames = 9
    return frames


def _scene_prompt(cfg: dict) -> str:
    """Combine global_prompt + specific_prompts[0] into a single prompt."""
    parts: list[str] = []
    if cfg.get("global_prompt"):
        parts.append(cfg["global_prompt"].strip())
    sp = cfg.get("specific_prompts") or []
    if sp:
        parts.append(sp[0].strip())
    return "\n\n".join(parts)


def _discover_scenes(bench_dir: Path) -> list[tuple[str, dict, Path]]:
    out: list[tuple[str, dict, Path]] = []
    for scene_dir in sorted(bench_dir.iterdir()):
        if not scene_dir.is_dir():
            continue
        cfg_path = scene_dir / "config.json"
        if not cfg_path.exists():
            continue
        with cfg_path.open() as f:
            cfg = json.load(f)
        seed_img = scene_dir / cfg.get("images", [{}])[0].get("path", "start_frame.png")
        if not seed_img.exists():
            print(f"  ! skipping {scene_dir.name}: seed image missing ({seed_img})")
            continue
        out.append((scene_dir.name, cfg, seed_img))
    return out


def _run_inference(
    *,
    base_checkpoint: Path,
    text_encoder_path: Path,
    adapter: Path,
    seed_image: Path,
    prompt: str,
    negative_prompt: str,
    width: int,
    height: int,
    num_frames: int,
    fps: float,
    seed: int,
    output: Path,
) -> bool:
    """Run inference.py once. Returns True on success, False on non-zero exit."""
    output.parent.mkdir(parents=True, exist_ok=True)
    repo_root = Path(__file__).resolve().parent.parent
    trainer_dir = repo_root / "packages" / "ltx-trainer"
    cmd = [
        "uv", "run", "python", "scripts/inference.py",
        "--checkpoint", str(base_checkpoint),
        "--text-encoder-path", str(text_encoder_path),
        "--lora-path", str(adapter),
        "--condition-image", str(seed_image),
        "--prompt", prompt,
        "--negative-prompt", negative_prompt,
        "--width", str(width),
        "--height", str(height),
        "--num-frames", str(num_frames),
        "--frame-rate", str(fps),
        "--num-inference-steps", str(DEFAULT_INFERENCE_STEPS),
        "--guidance-scale", str(DEFAULT_GUIDANCE),
        "--stg-scale", str(DEFAULT_STG),
        "--seed", str(seed),
        "--output", str(output),
    ]
    print("  → running inference.py")
    try:
        subprocess.run(cmd, cwd=trainer_dir, check=True)
    except subprocess.CalledProcessError as exc:
        print(f"  ✗ inference failed (exit {exc.returncode})")
        return False
    return True


def _build_html(out_dir: Path, scenes: list[tuple[str, dict, Path]], adapters: list[Path]) -> Path:
    """Build a side-by-side comparison page: rows = scenes, columns = weight pairs."""
    html_dir = out_dir / "blind_comparison"
    html_dir.mkdir(parents=True, exist_ok=True)
    html_path = html_dir / "index.html"

    lines: list[str] = [
        "<!doctype html><meta charset=utf-8>",
        "<title>Skye combined v1 — full sweep</title>",
        "<style>",
        "body{font-family:sans-serif;background:#111;color:#eee;padding:24px;max-width:1800px;margin:auto}",
        "h1,h2{font-weight:300} h2{margin-top:32px;border-top:1px solid #333;padding-top:24px}",
        f".grid{{display:grid;grid-template-columns:repeat({len(adapters)},1fr);gap:16px}}",
        "figure{margin:0;background:#1c1c1c;padding:12px;border-radius:6px}",
        "figcaption{margin-top:8px;font-size:13px;color:#aaa}",
        "video{width:100%;border-radius:4px}",
        "details{background:#1c1c1c;padding:8px 16px;border-radius:6px;margin-top:8px}",
        "summary{cursor:pointer;color:#aaa;font-size:13px}",
        "pre{white-space:pre-wrap;font-size:12px;color:#bbb;margin:8px 0 0}",
        ".note{color:#888;font-size:13px;margin-top:24px;line-height:1.5}",
        "</style>",
        "<h1>Skye combined v1 — Phase 3 sweep</h1>",
        "<p>Each row is a benchmark scene. Each column is a different (w_visual, w_motion) merge.</p>",
    ]

    for scene_name, cfg, _ in scenes:
        lines.append(f"<h2>{escape(scene_name)}</h2>")
        if cfg.get("description"):
            lines.append(f"<p style='color:#ccc'>{escape(cfg['description'])}</p>")
        lines.append("<div class=grid>")
        for adapter in adapters:
            adapter_name = adapter.stem
            video_rel = f"../{scene_name}/{adapter_name}.mp4"
            label = adapter_name.replace("lora_", "")
            lines.append(
                f"<figure><video controls preload=metadata src='{escape(video_rel)}'></video>"
                f"<figcaption>{escape(label)}</figcaption></figure>"
            )
        lines.append("</div>")
        lines.append(
            "<details><summary>Prompt</summary>"
            f"<pre>{escape(_scene_prompt(cfg))}</pre></details>"
        )

    lines.append("<p class=note>Generated by run_bm_sweep.py.</p>")
    html_path.write_text("\n".join(lines))
    return html_path


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bench-dir", required=True, type=Path)
    p.add_argument("--adapter-dir", required=True, type=Path)
    p.add_argument("--base-checkpoint", required=True, type=Path)
    p.add_argument("--text-encoder-path", required=True, type=Path)
    p.add_argument("--out-dir", required=True, type=Path)
    p.add_argument(
        "--max-frames",
        type=int,
        default=None,
        help="Cap total frames per clip (overrides scene's duration*fps). "
             "Useful if longer clips OOM on the GPU.",
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    scenes = _discover_scenes(args.bench_dir)
    if not scenes:
        print(f"No scenes found under {args.bench_dir}", file=sys.stderr)
        return 1
    adapters = sorted(args.adapter_dir.glob("*.safetensors"))
    if not adapters:
        print(f"No adapters found under {args.adapter_dir}", file=sys.stderr)
        return 1

    print(f"Found {len(scenes)} scenes and {len(adapters)} adapters → {len(scenes) * len(adapters)} clips")
    for s, _, _ in scenes:
        print(f"  scene: {s}")
    for a in adapters:
        print(f"  adapter: {a.name}")

    if args.dry_run:
        print("--dry-run: not running inference")
        _build_html(args.out_dir, scenes, adapters)
        return 0

    failed: list[tuple[str, str]] = []
    for scene_name, cfg, seed_img in scenes:
        scene_out = args.out_dir / scene_name
        for adapter in adapters:
            out_file = scene_out / f"{adapter.stem}.mp4"
            if out_file.exists() and out_file.stat().st_size > 0:
                print(f"  ✓ exists, skipping: {out_file.relative_to(args.out_dir)}")
                continue
            print(f"\n=== {scene_name} × {adapter.name} ===")
            duration = float(cfg.get("duration", 4.0))
            fps = float(cfg.get("fps", DEFAULT_FPS))
            num_frames = _frames_for_duration(duration, fps)
            if args.max_frames:
                num_frames = min(num_frames, args.max_frames)
                # Re-snap to k*8+1 in case max_frames doesn't satisfy
                num_frames = ((num_frames - 1) // 8) * 8 + 1

            dims = cfg.get("dimensions") or {}
            width = int(dims.get("width", 960))
            height = int(dims.get("height", 544))
            seed = int(cfg.get("seed", 42))
            negative = cfg.get("negative_prompt", "")
            prompt = _scene_prompt(cfg)

            ok = _run_inference(
                base_checkpoint=args.base_checkpoint,
                text_encoder_path=args.text_encoder_path,
                adapter=adapter,
                seed_image=seed_img,
                prompt=prompt,
                negative_prompt=negative,
                width=width,
                height=height,
                num_frames=num_frames,
                fps=fps,
                seed=seed,
                output=out_file,
            )
            if not ok:
                failed.append((scene_name, adapter.name))

    html_path = _build_html(args.out_dir, scenes, adapters)
    print(f"\nComparison HTML: {html_path}")
    if failed:
        print(f"\n{len(failed)} pairs failed:")
        for s, a in failed:
            print(f"  - {s} × {a}")
        return 2
    print("\nAll pairs succeeded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
