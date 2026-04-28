#!/usr/bin/env python3
"""
build_lora_benchmark_viewer.py

Build per-scene checkpoint comparison viewers for any LoRA benchmark whose
on-disk layout matches:

    <bench_root>/_base/<scene>.mp4              -> "base"           (no LoRA)
    <bench_root>/step_<N>/<scene>_lora.mp4      -> "step_<N>"       (LoRA at step N)

For every scene, runs vlm_pipeline.run (cheap/Flash mode) on each checkpoint
and writes a flat-list manifest.json that the existing auto_val viewer.html
consumes.

Per-bench output:
    auto_val/outputs_vlm_<bench-slug>_<scene>/
      manifest.json                 (flat list of {model, folder, video, score, ...})
      viewer.html                   (copy of vlm_pipeline/viewer.html)
      <per-video VLM run subdirs>/

Run examples:
    cd /Users/efrattaig/projects/sm/LTX-2/auto_val
    GOOGLE_CLOUD_PROJECT=shapeshifter-459611 \\
    GOOGLE_GENAI_USE_VERTEXAI=True \\
    GOOGLE_CLOUD_LOCATION=global \\
    python build_lora_benchmark_viewer.py --bench goldenplus150
    python build_lora_benchmark_viewer.py --bench full_cast_exp1 --scene olaf
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT  = Path("/Users/efrattaig/projects/sm/LTX-2")
AUTO_VAL   = REPO_ROOT / "auto_val"
LORA_ROOT  = REPO_ROOT / "lora_results"
BM_V1_ROOT = REPO_ROOT / "inputs" / "BM_v1" / "benchmark_v1"

# Each entry: bench-slug -> (bench_root, [(label, folder, filename_template), ...])
# filename_template uses "{scene}" which expands to the scene stem.
BENCHMARKS: dict[str, dict] = {
    "goldenplus150": {
        "root":   LORA_ROOT / "goldenPlus150_version1" / "goldenPLUS150_benchmark",
        "out_prefix": "outputs_vlm_goldenplus150_",
        "checkpoints": [
            ("base",        "_base",       "{scene}.mp4"),
            ("step_10000",  "step_10000",  "{scene}_lora.mp4"),
            ("step_15000",  "step_15000",  "{scene}_lora.mp4"),
            ("step_25000",  "step_25000",  "{scene}_lora.mp4"),
            ("step_35000",  "step_35000",  "{scene}_lora.mp4"),
            ("step_40000",  "step_40000",  "{scene}_lora.mp4"),
        ],
    },
    "full_cast_exp1": {
        "root":   LORA_ROOT / "full_episods" / "full_cast_exp1_benchmark",
        "out_prefix": "outputs_vlm_full_cast_exp1_",
        "checkpoints": [
            ("base",        "_base",       "{scene}.mp4"),
            ("step_03500",  "step_03500",  "{scene}_lora.mp4"),
            ("step_06000",  "step_06000",  "{scene}_lora.mp4"),
            ("step_08500",  "step_08500",  "{scene}_lora.mp4"),
            ("step_10000",  "step_10000",  "{scene}_lora.mp4"),
        ],
    },
}


def discover_scenes(bench_root: Path) -> list[str]:
    """Scenes are the .mp4 stems present in <bench>/_base/."""
    base_dir = bench_root / "_base"
    if not base_dir.exists():
        raise FileNotFoundError(f"No _base/ under {bench_root}")
    return sorted(p.stem for p in base_dir.glob("*.mp4"))


def load_prompt(scene: str) -> str:
    """Look up the canonical prompt for a scene from BM_v1 config.json."""
    cfg = BM_V1_ROOT / scene / "config.json"
    if not cfg.exists():
        return ""
    data = json.loads(cfg.read_text())
    prompts = data.get("specific_prompts") or []
    if not prompts:
        return ""
    return prompts[0] if isinstance(prompts, list) else str(prompts)


def run_vlm(video: Path, prompt: str, out_dir: Path) -> Path | None:
    """Run vlm_pipeline.run on `video`. Returns the new per-video subdir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = out_dir / f"_prompt_{video.stem}.txt"
    prompt_file.write_text(prompt)

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

    new = [p for p in out_dir.iterdir() if p.is_dir() and p.name not in existing]
    return new[0] if new else None


def process_scene(bench_cfg: dict, scene: str, force: bool) -> dict | None:
    bench_root: Path = bench_cfg["root"]
    out_prefix: str  = bench_cfg["out_prefix"]
    checkpoints     = bench_cfg["checkpoints"]

    prompt = load_prompt(scene)
    if not prompt:
        print(f"[{scene}] no prompt found in BM_v1 config — skip", flush=True)
        return None

    out = AUTO_VAL / f"{out_prefix}{scene}"
    if force and out.exists():
        print(f"\n[{scene}] --force: removing previous output", flush=True)
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    viewer_src = AUTO_VAL / "vlm_pipeline" / "viewer.html"
    if viewer_src.exists():
        shutil.copy2(viewer_src, out / "viewer.html")

    print(f"\n=== {scene} ===", flush=True)

    # Resume support: keep entries already present in manifest.json
    manifest_path = out / "manifest.json"
    existing_manifest: list[dict] = []
    if manifest_path.exists() and not force:
        try:
            existing_manifest = json.loads(manifest_path.read_text())
        except Exception:
            existing_manifest = []
    done_models = {m["model"] for m in existing_manifest}

    entries = list(existing_manifest)

    for label, ckpt_dir, fname_tpl in checkpoints:
        if label in done_models:
            print(f"  [{label}] already in manifest — skip", flush=True)
            continue
        video = bench_root / ckpt_dir / fname_tpl.format(scene=scene)
        if not video.exists() or video.stat().st_size == 0:
            print(f"  [{label}] missing {video} — skip", flush=True)
            continue

        # Stage with a label-prefixed name so per-run subdirs are readable.
        staged = out / f"{label}__{scene}.mp4"
        shutil.copy2(video, staged)

        sub = run_vlm(staged, prompt, out)
        staged.unlink(missing_ok=True)
        if sub is None:
            continue

        report_path = sub / "report.json"
        if not report_path.exists():
            print(f"    no report.json in {sub.name} — skip", flush=True)
            continue
        report = json.loads(report_path.read_text())
        agg = report.get("aggregator", {})
        mp4s = list(sub.glob("*.mp4"))
        if not mp4s:
            print(f"    no mp4 found in {sub.name} — skip", flush=True)
            continue

        entries.append({
            "model":    label,
            "folder":   sub.name,
            "video":    mp4s[0].name,
            "score":    int(agg.get("score", 0)),
            "verdict":  agg.get("overall_verdict", "FAIL"),
            "headline": agg.get("headline", ""),
        })
        # Persist incrementally so a crash doesn't lose work.
        manifest_path.write_text(json.dumps(entries, indent=2))

    manifest_path.write_text(json.dumps(entries, indent=2))
    print(f"\n  [{scene}] {len(entries)}/{len(checkpoints)} done -> {manifest_path}",
          flush=True)
    return {"scene": scene, "entries": entries}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--bench", required=True, choices=sorted(BENCHMARKS.keys()),
                   help="Which benchmark to process.")
    p.add_argument("--scene", action="append", default=None,
                   help="Limit to one or more scenes (repeatable). "
                        "Default: all scenes found under <bench>/_base/.")
    p.add_argument("--force", action="store_true",
                   help="Wipe and re-run each scene's output dir.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    bench_cfg = BENCHMARKS[args.bench]
    scenes = args.scene or discover_scenes(bench_cfg["root"])
    n_ckpt = len(bench_cfg["checkpoints"])

    print(f"=== build_lora_benchmark_viewer.py — {args.bench} "
          f"({len(scenes)} scenes × {n_ckpt} checkpoints) ===", flush=True)
    for s in scenes:
        print(f"  - {s}", flush=True)

    results = []
    for scene in scenes:
        r = process_scene(bench_cfg, scene, force=args.force)
        if r is not None:
            results.append(r)

    print(f"\n=== DONE — {len(results)}/{len(scenes)} scenes processed ===", flush=True)
    for r in results:
        n = len(r["entries"])
        scores = ", ".join(f"{e['model']}:{e['score']}" for e in r["entries"])
        print(f"  {r['scene']:48} {n}/{n_ckpt}  [{scores}]", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
