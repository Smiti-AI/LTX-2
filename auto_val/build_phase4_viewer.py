#!/usr/bin/env python3
"""
build_phase4_viewer.py

Assembles a 3-way model comparison viewer for Phase 4 results.

For every scene (BM_v1 + chase_bm + sky_bm = 17 scenes), shows:
- 150     = goldenPLUS150 LoRA (cumulative step 10000, pre-overfit best per diagnosis)
- episode = full_cast_exp1_season1 LoRA (cumulative step 10000, final)
- base    = LTX-2.3 base, no LoRA

For each video, runs vlm_pipeline.run in cheap (Flash) mode, producing a report.json
with score + verdict + headline. Aggregates into per-scene manifest.json files
that the existing auto_val viewer.html can render.

Run:
    cd /Users/efrattaig/projects/sm/LTX-2/auto_val
    uv run python build_phase4_viewer.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
import importlib.util

REPO_ROOT = Path("/Users/efrattaig/projects/sm/LTX-2")
AUTO_VAL = REPO_ROOT / "auto_val"
LORA_RESULTS = REPO_ROOT / "lora_results"

# Local paths for each benchmark's results (post-rsync)
BM_V1_GOLDENPLUS_DIR    = LORA_RESULTS / "goldenPlus150_version1" / "goldenPLUS150_benchmark"
BM_V1_FULLCAST_DIR      = LORA_RESULTS / "full_episods" / "full_cast_exp1_benchmark"

CHASE_GOLDENPLUS_DIR    = LORA_RESULTS / "chase_lora" / "benchmarks" / "goldenPLUS150_benchmark"
CHASE_FULLCAST_DIR      = LORA_RESULTS / "chase_lora" / "benchmarks" / "full_cast_exp1_benchmark"
CHASE_BASE_DIR          = LORA_RESULTS / "chase_lora" / "benchmarks" / "_base"

SKYE_GOLDENPLUS_DIR     = LORA_RESULTS / "skye_lora_output" / "benchmarks" / "goldenPLUS150_benchmark"
SKYE_FULLCAST_DIR       = LORA_RESULTS / "skye_lora_output" / "benchmarks" / "full_cast_exp1_benchmark"
SKYE_BASE_DIR           = LORA_RESULTS / "skye_lora_output" / "benchmarks" / "_base"

# Phase 3 step naming — goldenPLUS150 has 5 cumulative-renamed checkpoints; pick step_10000 (best per diagnosis)
GOLDENPLUS_STEP = 10000
FULLCAST_STEP   = 10000   # full_cast_exp1_season1 cumulative final


def load_chase_scenes() -> list[dict]:
    """Import run_chase_benchmark and return BENCHMARK_SCENES (name + prompt)."""
    spec = importlib.util.spec_from_file_location("rcb", REPO_ROOT / "run_chase_benchmark.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        # Fallback: parse manually if module fails to load
        return []
    return [{"name": s["name"], "prompt": s["prompt"]} for s in mod.BENCHMARK_SCENES]


def load_skye_scenes() -> list[dict]:
    """Import run_skye_benchmark and return BENCHMARK_SCENES (name + prompt)."""
    spec = importlib.util.spec_from_file_location("rsb", REPO_ROOT / "run_skye_benchmark.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return []
    return [{"name": s["name"], "prompt": s["prompt"]} for s in mod.BENCHMARK_SCENES]


def load_bm_v1_scenes() -> list[dict]:
    """Read prompts from inputs/BM_v1/benchmark_v1/<scene>/config.json."""
    bm_root = REPO_ROOT / "inputs" / "BM_v1" / "benchmark_v1"
    scenes = []
    for d in sorted(bm_root.iterdir()):
        if not d.is_dir():
            continue
        cfg = d / "config.json"
        if not cfg.exists():
            continue
        data = json.loads(cfg.read_text())
        prompts = data.get("specific_prompts", [])
        if not prompts:
            continue
        # First prompt is the canonical scene prompt
        prompt = prompts[0] if isinstance(prompts, list) else prompts
        scenes.append({"name": d.name, "prompt": prompt})
    return scenes


def find_video(*candidate_paths: Path) -> Path | None:
    """Return the first existing path among candidates."""
    for p in candidate_paths:
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def run_vlm(video: Path, prompt: str, out_dir: Path) -> Path | None:
    """Run vlm_pipeline.run on video. Returns the per-model subfolder created."""
    prompt_file = out_dir / f"_prompt_{video.stem}.txt"
    out_dir.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text(prompt)

    # Snapshot existing subfolders so we can identify the new one
    existing = {p.name for p in out_dir.iterdir() if p.is_dir()}

    cmd = [
        sys.executable, "-m", "vlm_pipeline.run",
        str(video),
        "--prompt-file", str(prompt_file),
        "--out-dir", str(out_dir),
        "--mode", "cheap",
    ]
    print(f"  → {video.name}", flush=True)
    try:
        subprocess.run(cmd, check=True, cwd=str(AUTO_VAL))
    except subprocess.CalledProcessError as e:
        print(f"    VLM failed: {e}", flush=True)
        return None

    new_dirs = [p for p in out_dir.iterdir() if p.is_dir() and p.name not in existing]
    if not new_dirs:
        return None
    return new_dirs[0]


def process_scene(scene_name: str, prompt: str, model_videos: dict[str, Path]) -> dict | None:
    """Process one scene: run VLM on each model's video, build per-scene manifest."""
    scene_out = AUTO_VAL / f"outputs_vlm_compare_phase4_{scene_name}"
    if scene_out.exists():
        print(f"\n[{scene_name}] cleaning previous output", flush=True)
        shutil.rmtree(scene_out)
    scene_out.mkdir(parents=True)

    # Copy viewer.html into the scene dir
    viewer_src = AUTO_VAL / "vlm_pipeline" / "viewer.html"
    if viewer_src.exists():
        shutil.copy2(viewer_src, scene_out / "viewer.html")

    print(f"\n=== {scene_name} ===", flush=True)
    entries = []
    for model_name, video in model_videos.items():
        if video is None:
            print(f"  [{model_name}] missing video — skip", flush=True)
            continue
        # Stage video into model-named subdir for VLM tool
        staged = scene_out / f"_{model_name}_input.mp4"
        shutil.copy2(video, staged)

        sub = run_vlm(staged, prompt, scene_out)
        if sub is None:
            continue

        # Read the report.json that the VLM tool produced
        rpt = sub / "report.json"
        if not rpt.exists():
            print(f"    no report.json in {sub.name}", flush=True)
            continue
        data = json.loads(rpt.read_text())
        agg = data.get("aggregator", {})

        # Find the video file the VLM tool emitted in its subfolder
        mp4s = list(sub.glob("*.mp4"))
        emitted = mp4s[0].name if mp4s else staged.name

        entries.append({
            "model": model_name,
            "folder": sub.name,
            "video": emitted,
            "score": int(agg.get("score", 0)),
            "verdict": agg.get("overall_verdict", "FAIL"),
            "headline": agg.get("headline", ""),
        })
        # Cleanup staged copy (the VLM tool already has its own copy in the subfolder)
        if staged.exists():
            staged.unlink()

    # Write manifest.json
    manifest = {
        "scene": scene_name,
        "prompt": prompt,
        "models": entries,
    }
    (scene_out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"  → {scene_out.name}/manifest.json ({len(entries)} models)", flush=True)
    return manifest


def main() -> int:
    print("=== build_phase4_viewer.py ===", flush=True)
    print("Loading scene definitions…", flush=True)

    bm_v1_scenes = load_bm_v1_scenes()
    chase_scenes = load_chase_scenes()
    skye_scenes  = load_skye_scenes()

    print(f"  BM_v1   : {len(bm_v1_scenes)} scenes", flush=True)
    print(f"  chase_bm: {len(chase_scenes)} scenes", flush=True)
    print(f"  sky_bm  : {len(skye_scenes)} scenes", flush=True)

    all_results = []

    # ── BM_v1 scenes ──
    for sc in bm_v1_scenes:
        videos = {
            "150":     find_video(BM_V1_GOLDENPLUS_DIR / f"step_{GOLDENPLUS_STEP:05d}" / f"{sc['name']}_lora.mp4"),
            "episode": find_video(BM_V1_FULLCAST_DIR  / f"step_{FULLCAST_STEP:05d}"   / f"{sc['name']}_lora.mp4"),
            "base":    find_video(BM_V1_GOLDENPLUS_DIR / "_base" / f"{sc['name']}.mp4",
                                  BM_V1_FULLCAST_DIR  / "_base" / f"{sc['name']}.mp4"),
        }
        m = process_scene(sc["name"], sc["prompt"], videos)
        if m: all_results.append(m)

    # ── chase_bm scenes ──
    for sc in chase_scenes:
        videos = {
            "150":     find_video(CHASE_GOLDENPLUS_DIR / f"step_{GOLDENPLUS_STEP:05d}" / f"{sc['name']}_lora.mp4"),
            "episode": find_video(CHASE_FULLCAST_DIR   / f"step_{FULLCAST_STEP:05d}"   / f"{sc['name']}_lora.mp4"),
            "base":    find_video(CHASE_BASE_DIR / f"{sc['name']}.mp4"),
        }
        m = process_scene(f"chase_{sc['name']}", sc["prompt"], videos)
        if m: all_results.append(m)

    # ── sky_bm scenes ──
    for sc in skye_scenes:
        videos = {
            "150":     find_video(SKYE_GOLDENPLUS_DIR / f"step_{GOLDENPLUS_STEP:05d}" / f"{sc['name']}_lora.mp4"),
            "episode": find_video(SKYE_FULLCAST_DIR   / f"step_{FULLCAST_STEP:05d}"   / f"{sc['name']}_lora.mp4"),
            "base":    find_video(SKYE_BASE_DIR / f"{sc['name']}.mp4"),
        }
        m = process_scene(f"sky_{sc['name']}", sc["prompt"], videos)
        if m: all_results.append(m)

    # Write top-level summary
    summary_path = AUTO_VAL / "phase4_summary.json"
    summary_path.write_text(json.dumps([
        {"scene": r["scene"], "models": [m["model"] for m in r["models"]],
         "scores": {m["model"]: m["score"] for m in r["models"]}}
        for r in all_results
    ], indent=2))

    print(f"\n=== DONE — {len(all_results)} scenes processed ===", flush=True)
    print(f"Open viewers under {AUTO_VAL}/outputs_vlm_compare_phase4_<scene>/viewer.html", flush=True)
    print(f"Summary: {summary_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
