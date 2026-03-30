#!/usr/bin/env python3
"""
Batch LTX inference over `input/benchmark_v1` scenarios: each prompt is run **twice**
(with the LoRA, then with the base LTX-2 checkpoint only). Outputs share one folder per scenario.

  cd packages/ltx-trainer
  uv run python scripts/efrat_run_lora.py

Edit CONFIG below. Set `SCENARIO_NAMES` to restrict which benchmark subfolders run; leave empty
to process every subfolder of BENCHMARK_DIR. Outputs: `prompt_XX_lora.*` and `prompt_XX_base.*`
under OUTPUT_BASE / <scenario_name> /.

Per scenario: prompts, negative prompt, seed, fps, duration, and dimensions come from that folder’s
`config.json`. The conditioning image is `start_frame.png` in that same folder (or `images[0].path`
under the config if `start_frame.png` is absent). Optional `PROMPT_SUFFIX` / `FALLBACK_CONDITION_IMAGE`
at the bottom of CONFIG are off by default so behavior matches the benchmark files only.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# =============================================================================
# CONFIG — edit these values
# =============================================================================

# Repository root (LTX-2). Default: three levels up from this file.
REPO_ROOT: Path = Path(__file__).resolve().parents[3]

# Base transformer checkpoint (same as training `model_path`).
CHECKPOINT: Path = REPO_ROOT / "models" / "ltx-2.3-22b-dev.safetensors"

# Gemma text encoder directory (same as training `text_encoder_path`).
TEXT_ENCODER_PATH: Path = REPO_ROOT / "models" / "gemma-3-12b-it-qat-q4_0-unquantized"

# Fixed LoRA checkpoint (step 2000).
LORA_PATH: Path = (
    REPO_ROOT
    / "packages"
    / "ltx-trainer"
    / "outputs"
    / "skye_golden_av_lora"
    / "checkpoints"
    / "lora_weights_step_02000.safetensors"
)

# Benchmark scenarios (each subdir may contain config.json + optional start_frame.png, etc.).
BENCHMARK_DIR: Path = REPO_ROOT / "input" / "benchmark_v1"

# Only these subfolder names under BENCHMARK_DIR are run (order preserved).
# Leave empty to run all subdirectories that contain config.json.
SCENARIO_NAMES: list[str] = [
    "skye_helicopter_birthday_gili",
    "skye_chase_trampoline_backflip",
]

# All videos / sidecar audio land here: OUTPUT_BASE / <scenario_name> / ...
OUTPUT_BASE: Path = REPO_ROOT / "output" / "lora_res" / "lora_skye_golden_v2"

# Inference defaults — aligned with repo `run_skye_video.sh` (same CLI as scripts/inference.py).
NUM_INFERENCE_STEPS: int = 40
GUIDANCE_SCALE: float = 4.0
STG_SCALE: float = 1.0
STG_BLOCKS: list[int] = [29]
STG_MODE: str = "stg_av"  # "stg_av" | "stg_v"
ENHANCE_PROMPT: bool = False
SKIP_AUDIO: bool = False
INCLUDE_REFERENCE_IN_OUTPUT: bool = False
REFERENCE_VIDEO: Path | None = None  # set for V2V; benchmark configs do not use this by default
DEVICE: str = "cuda"

# If set, overrides benchmark `fps` (run_skye_video.sh uses 25.0; many benchmark JSONs use 24).
FRAME_RATE_OVERRIDE: float | None = None

# Extra text appended after the JSON prompt (empty = prompts are exactly from config.json).
# Example (run_skye_video.sh style): " static camera composition maintained for the entire clip"
PROMPT_SUFFIX: str = ""

# If set, used only when `start_frame.png` and config `images[0]` paths are missing. None = no fallback.
FALLBACK_CONDITION_IMAGE: Path | None = None

# If True, combine each entry in `specific_prompts` with `global_prompt` (recommended).
COMBINE_GLOBAL_AND_SPECIFIC_PROMPTS: bool = True

# =============================================================================
# Benchmark parsing helpers
# =============================================================================


def snap_multiple_of_32(n: int, *, minimum: int = 32) -> int:
    """LTX requires height/width divisible by 32."""
    return max(minimum, ((int(n) + 31) // 32) * 32)


def num_frames_from_duration(duration_sec: float, fps: float) -> int:
    """Same as get_frames() in run_skye_video.sh: raw=round(sec*fps), k=int((raw-1)/8+0.5), n=8*k+1, min 17."""
    raw = max(1, int(round(float(duration_sec) * float(fps))))
    k = int((raw - 1) / 8 + 0.5)
    n = 8 * k + 1
    return max(17, n)


def load_benchmark_config(scenario_dir: Path) -> dict:
    path = scenario_dir / "config.json"
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def resolve_condition_image(scenario_dir: Path, cfg: dict, fallback: Path | None) -> Path | None:
    """Use each scenario's `start_frame.png` first, then config `images[0].path`, then optional fallback."""
    start_default = scenario_dir / "start_frame.png"
    if start_default.is_file():
        return start_default.resolve()

    images = cfg.get("images") or []
    if images:
        rel = images[0].get("path")
        if rel:
            p = (scenario_dir / rel).resolve()
            if p.is_file():
                return p
            print(f"  Warning: conditioning image missing: {p}")

    if fallback is not None:
        fb = fallback.expanduser().resolve()
        if fb.is_file():
            print(f"  Using fallback conditioning image: {fb}")
            return fb

    print("  No usable conditioning image; continuing with text-to-video.")
    return None


def build_full_prompt(cfg: dict, specific: str) -> str:
    global_p = (cfg.get("global_prompt") or "").strip()
    spec = specific.strip()
    if COMBINE_GLOBAL_AND_SPECIFIC_PROMPTS and global_p:
        text = f"{global_p}\n\n{spec}"
    else:
        text = spec
    if PROMPT_SUFFIX:
        text = text.rstrip() + PROMPT_SUFFIX
    return text


def iter_scenario_dirs(benchmark_dir: Path, only_names: list[str]) -> list[Path]:
    if not benchmark_dir.is_dir():
        raise FileNotFoundError(f"BENCHMARK_DIR not found: {benchmark_dir}")
    if only_names:
        out: list[Path] = []
        for name in only_names:
            p = benchmark_dir / name
            if not p.is_dir():
                raise FileNotFoundError(f"Scenario folder not found: {p}")
            out.append(p)
        return out
    return sorted(d for d in benchmark_dir.iterdir() if d.is_dir())


def run_one_inference(
    *,
    inference_script: Path,
    lora: Path | None,
    prompt: str,
    negative_prompt: str,
    output_mp4: Path,
    seed: int,
    height: int,
    width: int,
    num_frames: int,
    frame_rate: float,
    condition_image: Path | None,
    audio_output: Path | None,
) -> int:
    cmd: list[str] = [
        sys.executable,
        str(inference_script),
        "--checkpoint",
        str(CHECKPOINT),
        "--text-encoder-path",
        str(TEXT_ENCODER_PATH),
    ]
    if lora is not None:
        cmd.extend(["--lora-path", str(lora)])
    cmd.extend(
        [
            "--prompt",
            prompt,
            "--negative-prompt",
            negative_prompt,
            "--output",
            str(output_mp4),
            "--height",
            str(height),
            "--width",
            str(width),
            "--num-frames",
            str(num_frames),
            "--frame-rate",
            str(frame_rate),
            "--num-inference-steps",
            str(NUM_INFERENCE_STEPS),
            "--guidance-scale",
            str(GUIDANCE_SCALE),
            "--stg-scale",
            str(STG_SCALE),
            "--stg-mode",
            STG_MODE,
            "--seed",
            str(seed),
            "--device",
            DEVICE,
        ]
    )
    cmd.append("--stg-blocks")
    cmd.extend(str(b) for b in STG_BLOCKS)

    if SKIP_AUDIO:
        cmd.append("--skip-audio")
    if ENHANCE_PROMPT:
        cmd.append("--enhance-prompt")
    if condition_image is not None:
        cmd.extend(["--condition-image", str(condition_image)])
    if REFERENCE_VIDEO is not None:
        cmd.extend(["--reference-video", str(REFERENCE_VIDEO)])
    if INCLUDE_REFERENCE_IN_OUTPUT:
        cmd.append("--include-reference-in-output")
    if audio_output is not None and not SKIP_AUDIO:
        cmd.extend(["--audio-output", str(audio_output)])

    return subprocess.call(cmd)


def main() -> None:
    inference_script = Path(__file__).resolve().parent / "inference.py"
    if not inference_script.is_file():
        raise FileNotFoundError(f"Expected inference.py next to this script: {inference_script}")

    lora = LORA_PATH.expanduser().resolve()
    if not lora.is_file():
        raise FileNotFoundError(f"LORA_PATH does not exist: {lora}")

    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)

    scenarios = iter_scenario_dirs(BENCHMARK_DIR, SCENARIO_NAMES)
    if not scenarios:
        raise FileNotFoundError(f"No scenario subdirectories under {BENCHMARK_DIR}")

    failed: list[str] = []

    for scenario_dir in scenarios:
        cfg_path = scenario_dir / "config.json"
        if not cfg_path.is_file():
            print(f"Skip (no config.json): {scenario_dir.name}")
            continue

        cfg = load_benchmark_config(scenario_dir)
        name = scenario_dir.name
        prompts: list[str] = cfg.get("specific_prompts") or []
        if not prompts:
            print(f"Skip (no specific_prompts): {name}")
            continue

        dim = cfg.get("dimensions") or {}
        raw_w = int(dim.get("width", 1280))
        raw_h = int(dim.get("height", 720))
        width = snap_multiple_of_32(raw_w)
        height = snap_multiple_of_32(raw_h)
        if width != raw_w or height != raw_h:
            print(f"  Note: snapped dimensions {raw_w}x{raw_h} -> {width}x{height} (multiple of 32)")

        duration = float(cfg.get("duration", 10))
        fps = float(FRAME_RATE_OVERRIDE if FRAME_RATE_OVERRIDE is not None else cfg.get("fps", 24))
        num_frames = num_frames_from_duration(duration, fps)

        neg = (cfg.get("negative_prompt") or "").strip()
        seed = int(cfg.get("seed", 42))
        condition_image = resolve_condition_image(scenario_dir, cfg, FALLBACK_CONDITION_IMAGE)

        out_dir = OUTPUT_BASE / name
        out_dir.mkdir(parents=True, exist_ok=True)

        for i, spec in enumerate(prompts):
            prompt = build_full_prompt(cfg, spec)
            tag = f"{i:02d}"

            runs: list[tuple[str, Path | None]] = [
                ("lora", lora),
                ("base", None),
            ]

            for label, lora_for_run in runs:
                output_mp4 = out_dir / f"prompt_{tag}_{label}.mp4"
                audio_out = out_dir / f"prompt_{tag}_{label}.wav"

                print("=" * 80)
                print(
                    f"Scenario: {name}  [{i + 1}/{len(prompts)}]  "
                    f"{'LoRA' if label == 'lora' else 'base LTX-2'} -> {output_mp4.name}"
                )
                print("=" * 80)

                code = run_one_inference(
                    inference_script=inference_script,
                    lora=lora_for_run,
                    prompt=prompt,
                    negative_prompt=neg,
                    output_mp4=output_mp4,
                    seed=seed,
                    height=height,
                    width=width,
                    num_frames=num_frames,
                    frame_rate=fps,
                    condition_image=condition_image,
                    audio_output=audio_out,
                )
                if code != 0:
                    failed.append(f"{name}/prompt_{tag}_{label} (exit {code})")

    if failed:
        print("\nFailures:")
        for f in failed:
            print(f"  - {f}")
        raise SystemExit(1)
    print("\nAll benchmark runs finished OK.")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
