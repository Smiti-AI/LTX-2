#!/usr/bin/env python3
"""
run_skye_like_api.py

The closest possible local equivalent to fal-ai/ltx-2.3/image-to-video/fast.

HOW THIS DIFFERS FROM run_skye_v0.py (and why it produces better results)
---------------------------------------------------------------------------
1. Uses DEFAULT_NEGATIVE_PROMPT (the library's built-in quality guide) instead
   of an empty string. The Fal API almost certainly uses this internally.
   An empty negative prompt means CFG compares against pure noise — far less
   effective than comparing against a list of known artifacts.

2. Reads LTX_2_3_HQ_PARAMS and DEFAULT_NEGATIVE_PROMPT directly from the
   library's source-of-truth constants instead of re-defining them. Any
   upstream update to the library will automatically apply here.

3. Accepts a BM_v1-style scene config.json directly, so you can run any
   benchmark scene with a single --scene argument.

4. Supports end-frame conditioning (end_frame.png) for start→end transitions,
   matching the Fal API's end_image_url feature.

5. Image conditioning strength is 1.0 by default (maximum start-frame
   adherence), configurable via --image-strength.

PIPELINE OVERVIEW (see README_LOCAL_INFERENCE.md for full explanation)
-----------------------------------------------------------------------
Stage 1  →  full 22B model at half resolution (960×544), 15 Res2s steps,
            CFG guidance (positive vs negative prompt), distilled LoRA at 0.25
Stage 2  →  2× spatial upsampler (960→1920, 544→1088), then 4-step distilled
            denoising with no CFG (SimpleDenoiser), distilled LoRA at 0.50

Required weights (all under MODELS_DIR):
    ltx-2.3-22b-dev.safetensors
    ltx-2.3-22b-distilled-lora-384.safetensors
    ltx-2.3-spatial-upscaler-x2-1.0.safetensors

Usage
-----
    # Basic — single scene config:
    uv run python run_skye_like_api.py \\
        --scene inputs/BM_v1/benchmark_v1/skye_helicopter_birthday_gili

    # Override prompt and duration:
    uv run python run_skye_like_api.py \\
        --scene inputs/BM_v1/benchmark_v1/skye_helicopter_birthday_gili \\
        --durations 6 8 10

    # Provide image + prompt manually (no scene config):
    uv run python run_skye_like_api.py \\
        --image path/to/start_frame.png \\
        --prompt "your prompt here" \\
        --durations 8

    # Skip audio (faster, same video quality):
    uv run python run_skye_like_api.py --scene <scene_dir> --no-audio
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from pathlib import Path

# ── paths ──────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
LTX_ROOT = SCRIPT_DIR

# All LTX-2.3 weights should live here.
MODELS_DIR = Path("/home/efrattaig/models/LTX-2.3")
CHECKPOINT       = MODELS_DIR / "ltx-2.3-22b-dev.safetensors"
DISTILLED_LORA   = MODELS_DIR / "ltx-2.3-22b-distilled-lora-384.safetensors"
SPATIAL_UPSAMPLER = MODELS_DIR / "ltx-2.3-spatial-upscaler-x2-1.0.safetensors"
GEMMA_ROOT       = Path("/home/efrattaig/models/gemma-3-12b-it-qat-q4_0-unquantized")

DEFAULT_OUTPUT_DIR = LTX_ROOT / "output"


# ── helpers ────────────────────────────────────────────────────────────────────

def frames_for_duration(seconds: float, fps: float) -> int:
    """Return the smallest num_frames = k*8+1 (≥17) that covers `seconds` at `fps`."""
    raw = round(seconds * fps)
    k = max(1, (raw - 1 + 7) // 8)
    return max(17, 8 * k + 1)


def load_scene(scene_dir: Path) -> dict:
    """Load and return a BM_v1 scene config.json."""
    cfg_path = scene_dir / "config.json"
    if not cfg_path.exists():
        raise FileNotFoundError(f"No config.json in {scene_dir}")
    with open(cfg_path) as f:
        return json.load(f)


def build_full_prompt(cfg: dict, prompt_index: int = 0) -> str:
    """Combine global_prompt + specific_prompt[i] exactly as the benchmark runner does."""
    global_p = cfg.get("global_prompt", "")
    specifics = cfg.get("specific_prompts") or []
    if not specifics:
        return global_p
    specific = specifics[min(prompt_index, len(specifics) - 1)]
    if global_p:
        return f"{global_p}\n\n{specific}"
    return specific


# ── pipeline build + run ───────────────────────────────────────────────────────

def build_pipeline(*, lora_str_s1: float, lora_str_s2: float, generate_audio: bool):
    """Construct TI2VidTwoStagesHQPipeline with the correct weights."""
    import torch
    # BigVGAN v2 (the audio vocoder) calls cuBLASLt convolutions that crash with
    # bitsandbytes on consumer GPUs.  Disabling cuDNN forces a plain cuBLAS path
    # that works fine.  The transformer uses flash-attention and is unaffected.
    torch.backends.cudnn.enabled = False

    from ltx_core.loader import LTXV_LORA_COMFY_RENAMING_MAP, LoraPathStrengthAndSDOps
    from ltx_pipelines.ti2vid_two_stages_hq import TI2VidTwoStagesHQPipeline

    distilled_lora = [
        LoraPathStrengthAndSDOps(
            path=str(DISTILLED_LORA),
            strength=1.0,           # nominal; per-stage strengths are set below
            sd_ops=LTXV_LORA_COMFY_RENAMING_MAP,
        )
    ]

    return TI2VidTwoStagesHQPipeline(
        checkpoint_path=str(CHECKPOINT),
        distilled_lora=distilled_lora,
        distilled_lora_strength_stage_1=lora_str_s1,
        distilled_lora_strength_stage_2=lora_str_s2,
        spatial_upsampler_path=str(SPATIAL_UPSAMPLER),
        gemma_root=str(GEMMA_ROOT),
        loras=(),
    )


def generate_one(
    pipeline,
    *,
    prompt: str,
    negative_prompt: str,
    seed: int,
    num_frames: int,
    width: int,
    height: int,
    fps: float,
    steps: int,
    video_guider_params,
    audio_guider_params,
    image_path: Path,
    end_image_path: Path | None,
    output_path: Path,
    generate_audio: bool,
    image_strength: float,
) -> None:
    import torch
    from ltx_core.model.video_vae import TilingConfig, get_video_chunks_number
    from ltx_core.types import Audio
    from ltx_pipelines.utils.args import ImageConditioningInput
    from ltx_pipelines.utils.media_io import encode_video

    tiling_config = TilingConfig.default()
    video_chunks_number = get_video_chunks_number(num_frames, tiling_config)

    # Silence the audio decoder when audio isn't requested, to avoid the
    # BigVGAN cuBLASLt crash on setups where audio isn't needed.
    if not generate_audio:
        class _SilentAudioDecoder:
            def __call__(self, latent: torch.Tensor) -> Audio:
                return Audio(waveform=torch.zeros(2, 0), sampling_rate=24000)
        pipeline.audio_decoder = _SilentAudioDecoder()

    images = [ImageConditioningInput(path=str(image_path), frame_idx=0, strength=image_strength)]
    if end_image_path is not None:
        # The pipeline treats any non-zero frame_idx as a keyframe target.
        # Use num_frames-1 to anchor the final frame.
        images.append(ImageConditioningInput(
            path=str(end_image_path),
            frame_idx=num_frames - 1,
            strength=image_strength,
        ))

    video, audio = pipeline(
        prompt=prompt,
        negative_prompt=negative_prompt,
        seed=seed,
        height=height,
        width=width,
        num_frames=num_frames,
        frame_rate=fps,
        num_inference_steps=steps,
        video_guider_params=video_guider_params,
        audio_guider_params=audio_guider_params,
        images=images,
        tiling_config=tiling_config,
    )

    with torch.inference_mode():
        encode_video(
            video=video,
            fps=int(fps),
            audio=audio if generate_audio else None,
            output_path=str(output_path),
            video_chunks_number=video_chunks_number,
        )


# ── main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # Import the library's own constants — single source of truth.
    # These are the exact values the HQ pipeline is tuned for.
    from ltx_core.components.guiders import MultiModalGuiderParams
    from ltx_pipelines.utils.constants import (
        DEFAULT_NEGATIVE_PROMPT,
        LTX_2_3_HQ_PARAMS,
    )

    p = LTX_2_3_HQ_PARAMS
    video_g = p.video_guider_params
    audio_g = p.audio_guider_params

    # ── stage-2 output resolution (stage 1 uses half)
    HQ_WIDTH  = p.stage_2_width   # 1920
    HQ_HEIGHT = p.stage_2_height  # 1088

    parser = argparse.ArgumentParser(
        description="Local LTX-2.3 HQ inference — closest local equivalent to fal-ai/ltx-2.3/image-to-video/fast.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # ── scene / image / prompt ──────────────────────────────────────────────────
    grp = parser.add_argument_group("Scene / input")
    grp.add_argument(
        "--scene", type=Path, default=None, metavar="DIR",
        help="Path to a BM_v1 scene folder containing config.json and start_frame.png. "
             "Overrides --image and --prompt when provided.",
    )
    grp.add_argument(
        "--image", type=Path, default=None, metavar="PATH",
        help="Start-frame image path (used when --scene is not provided).",
    )
    grp.add_argument(
        "--end-image", type=Path, default=None, metavar="PATH",
        help="End-frame image path for start→end transition (optional). "
             "Mirrors the Fal API's end_image_url parameter.",
    )
    grp.add_argument(
        "--prompt", type=str, default=None,
        help="Prompt text. Used when --scene is not provided, or to override the scene prompt.",
    )
    grp.add_argument(
        "--prompt-index", type=int, default=0,
        help="Which specific_prompt in the scene config to use (0 = first).",
    )
    grp.add_argument(
        "--negative-prompt", type=str, default=None,
        help="Negative prompt. Defaults to the library's DEFAULT_NEGATIVE_PROMPT — "
             "the same comprehensive artifact list that the API uses internally. "
             "Pass '' to disable (not recommended).",
    )

    # ── output ──────────────────────────────────────────────────────────────────
    grp = parser.add_argument_group("Output")
    grp.add_argument("--output-dir",  type=Path, default=DEFAULT_OUTPUT_DIR)
    grp.add_argument("--output-base", type=str, default=None,
                     help="Base name for output files. Defaults to scene name or 'skye_like_api'.")

    # ── timing ──────────────────────────────────────────────────────────────────
    grp = parser.add_argument_group("Timing")
    grp.add_argument(
        "--durations", type=float, nargs="+", default=[6.0, 8.0, 10.0],
        help="Duration(s) in seconds. The Fal API supports 6–20 s; 6/8/10 match the default API options.",
    )
    grp.add_argument(
        "--fps", type=float, default=25.0,
        help="Frames per second. 25 is the Fal API default. LTX supports 24/25/48/50.",
    )

    # ── resolution ──────────────────────────────────────────────────────────────
    grp = parser.add_argument_group("Resolution (stage-2 output)")
    grp.add_argument("--width",  type=int, default=HQ_WIDTH,
                     help="Stage-2 output width. Stage 1 uses width//2.")
    grp.add_argument("--height", type=int, default=HQ_HEIGHT,
                     help="Stage-2 output height. Stage 1 uses height//2.")

    # ── diffusion ───────────────────────────────────────────────────────────────
    grp = parser.add_argument_group("Diffusion (LTX_2_3_HQ_PARAMS defaults)")
    grp.add_argument(
        "--steps", type=int, default=p.num_inference_steps,
        help="Stage-1 denoising steps. 15 is the HQ default; Res2s is second-order "
             "so 15 ≈ 30 standard Euler steps in quality.",
    )
    grp.add_argument(
        "--video-cfg", type=float, default=video_g.cfg_scale,
        help="CFG scale for video. How strictly the model follows the prompt. "
             "3.0 = gentle guidance (image conditioning already anchors content).",
    )
    grp.add_argument(
        "--audio-cfg", type=float, default=audio_g.cfg_scale,
        help="CFG scale for audio. 7.0 = strict adherence (audio has no visual anchor).",
    )
    grp.add_argument(
        "--video-rescale", type=float, default=video_g.rescale_scale,
        help="Post-CFG rescale for video. Pulls back oversaturation. 0.45 is the HQ tuned value.",
    )
    grp.add_argument(
        "--audio-rescale", type=float, default=audio_g.rescale_scale,
        help="Post-CFG rescale for audio. 1.0 = no rescaling.",
    )
    grp.add_argument(
        "--a2v-scale", type=float, default=video_g.modality_scale,
        help="Audio→Video cross-attention scale. How much the audio shapes the video. "
             "3.0 improves lip-sync alignment.",
    )
    grp.add_argument(
        "--v2a-scale", type=float, default=audio_g.modality_scale,
        help="Video→Audio cross-attention scale. How much the video shapes the audio. "
             "3.0 improves audio-visual alignment.",
    )

    # ── LoRA ────────────────────────────────────────────────────────────────────
    grp = parser.add_argument_group("Distilled LoRA strengths")
    grp.add_argument(
        "--lora-s1", type=float, default=0.25,
        help="Distilled LoRA strength in Stage 1. Light touch (0.25) — just enough to "
             "help the Res2s sampler stay on the distilled trajectory.",
    )
    grp.add_argument(
        "--lora-s2", type=float, default=0.5,
        help="Distilled LoRA strength in Stage 2. Stronger (0.50) — Stage 2 only has 4 "
             "sigma steps so it needs more guidance to make each step count.",
    )

    # ── conditioning ────────────────────────────────────────────────────────────
    grp = parser.add_argument_group("Image conditioning")
    grp.add_argument(
        "--image-strength", type=float, default=1.0,
        help="How strongly the start frame constrains frame 0. "
             "1.0 = exact match; 0.9 = slight freedom. API default is 1.0.",
    )

    # ── misc ────────────────────────────────────────────────────────────────────
    grp = parser.add_argument_group("Misc")
    grp.add_argument("--seed", type=int, default=None,
                     help="Random seed. Omit for random (mirrors the API).")
    grp.add_argument("--no-audio", action="store_true",
                     help="Skip audio generation. Same video quality, faster.")

    args = parser.parse_args()

    # ── resolve inputs ──────────────────────────────────────────────────────────
    scene_cfg: dict = {}
    image_path: Path | None = args.image
    end_image_path: Path | None = args.end_image
    prompt: str | None = args.prompt
    output_base: str = args.output_base or "skye_like_api"

    if args.scene is not None:
        scene_dir = args.scene.resolve()
        scene_cfg = load_scene(scene_dir)
        output_base = args.output_base or scene_dir.name

        if image_path is None:
            image_path = scene_dir / "start_frame.png"

        if end_image_path is None:
            candidate = scene_dir / "end_frame.png"
            if candidate.exists():
                end_image_path = candidate

        if prompt is None:
            prompt = build_full_prompt(scene_cfg, args.prompt_index)

    if image_path is None:
        print("ERROR: provide --scene or --image", file=sys.stderr)
        return 1
    if prompt is None:
        print("ERROR: provide --scene or --prompt", file=sys.stderr)
        return 1
    if not image_path.exists():
        print(f"ERROR: start frame not found: {image_path}", file=sys.stderr)
        return 1

    negative_prompt = (
        args.negative_prompt
        if args.negative_prompt is not None
        else DEFAULT_NEGATIVE_PROMPT
    )

    # ── validate model weights ──────────────────────────────────────────────────
    missing = [p_ for p_ in (CHECKPOINT, DISTILLED_LORA, SPATIAL_UPSAMPLER, GEMMA_ROOT)
               if not p_.exists()]
    if missing:
        for m in missing:
            print(f"ERROR: weight not found: {m}", file=sys.stderr)
        return 1

    # ── guider params (read directly from LTX_2_3_HQ_PARAMS, allow CLI overrides) ──
    video_guider_params = MultiModalGuiderParams(
        cfg_scale=args.video_cfg,
        stg_scale=0.0,           # always off for HQ pipeline (API does the same)
        rescale_scale=args.video_rescale,
        modality_scale=args.a2v_scale,
        skip_step=0,
        stg_blocks=[],
    )
    audio_guider_params = MultiModalGuiderParams(
        cfg_scale=args.audio_cfg,
        stg_scale=0.0,
        rescale_scale=args.audio_rescale,
        modality_scale=args.v2a_scale,
        skip_step=0,
        stg_blocks=[],
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    generate_audio = not args.no_audio

    # ── print run summary ───────────────────────────────────────────────────────
    print("\nrun_skye_like_api — TI2VidTwoStagesHQPipeline")
    print(f"  checkpoint        : {CHECKPOINT.name}")
    print(f"  distilled LoRA    : {DISTILLED_LORA.name}  (s1={args.lora_s1}, s2={args.lora_s2})")
    print(f"  spatial upsampler : {SPATIAL_UPSAMPLER.name}")
    print(f"  start frame       : {image_path}")
    if end_image_path:
        print(f"  end frame         : {end_image_path}")
    print(f"  stage-1 res       : {args.width // 2} × {args.height // 2}  (Res2s, {args.steps} steps)")
    print(f"  stage-2 res       : {args.width} × {args.height}  (2× upsample + distilled LoRA)")
    print(f"  fps               : {args.fps}")
    print(f"  video CFG         : {args.video_cfg}   (video rescale={args.video_rescale})")
    print(f"  audio CFG         : {args.audio_cfg}   (audio rescale={args.audio_rescale})")
    print(f"  a2v/v2a scale     : {args.a2v_scale}")
    print(f"  STG               : disabled")
    print(f"  negative prompt   : {'DEFAULT (library built-in)' if args.negative_prompt is None else 'custom'}")
    print(f"  durations         : {args.durations} s")
    print(f"  audio             : {'yes' if generate_audio else 'no (--no-audio)'}")
    print()

    print("Loading pipeline…")
    pipeline = build_pipeline(
        lora_str_s1=args.lora_s1,
        lora_str_s2=args.lora_s2,
        generate_audio=generate_audio,
    )

    errors = 0
    for dur in args.durations:
        seed = args.seed if args.seed is not None else random.randint(0, 2**31 - 1)
        nf = frames_for_duration(dur, args.fps)
        actual_dur = round(nf / args.fps, 2)
        out_name = (
            f"run_skye_like_api__{output_base}__"
            f"{args.width}x{args.height}_{args.steps}steps_{actual_dur}s_seed{seed}.mp4"
        )
        output_path = args.output_dir / out_name

        print(f"Generating {dur}s → {nf} frames ({actual_dur}s) | seed={seed}")
        try:
            generate_one(
                pipeline,
                prompt=prompt,
                negative_prompt=negative_prompt,
                seed=seed,
                num_frames=nf,
                width=args.width,
                height=args.height,
                fps=args.fps,
                steps=args.steps,
                video_guider_params=video_guider_params,
                audio_guider_params=audio_guider_params,
                image_path=image_path,
                end_image_path=end_image_path,
                output_path=output_path,
                generate_audio=generate_audio,
                image_strength=args.image_strength,
            )
            print(f"  saved: {output_path}")
        except Exception:
            logging.exception("Generation failed for %ss", dur)
            errors += 1
        print()

    total = len(args.durations)
    print(f"Done. {total - errors}/{total} generation(s) succeeded.")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
