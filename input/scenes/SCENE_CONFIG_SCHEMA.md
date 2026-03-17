# Scene Config JSON Schema

Each scene folder in `input/scenes/` should contain:
- **config.json** – scene parameters
- **Images** – `start_frame.png` and optionally more keyframe images

## Required / Core Fields

| Field | Type | Description |
|-------|------|-------------|
| `global_prompt` | string | Shared style/quality prompt prepended or used as fallback |
| `specific_prompts` | string[] | Per-shot prompts; `[0]` = start, `[1]` = end (with dialogue/action) |
| `negative_prompt` | string | What to avoid (e.g. "blurry, low quality, distorted") |
| `duration` | number | Video duration in seconds (e.g. 5) |
| `dimensions` | object | `{ "width": 480, "height": 854 }` – must be divisible by 32 |
| `fps` | number | Frame rate (e.g. 24) |
| `seed` | number | Random seed for reproducibility (default: 42) |

## Optional Fields (Recommended for Benchmarks)

| Field | Type | Description |
|-------|------|-------------|
| `images` | array | Explicit image→frame mapping (see below) |
| `num_inference_steps` | number | Denoising steps (default: 30 for LTX-2.3) |
| `enhance_prompt` | boolean | Use Gemma to enhance prompt (default: false) |
| `description` | string | Human-readable scene name for reports |
| `skip` | boolean | Skip this scene in benchmark runs |

## Optional Fields (Advanced / Overrides)

| Field | Type | Description |
|-------|------|-------------|
| `motion_bucket` | number | Motion intensity hint (if supported by pipeline) |
| `video_cfg_guidance_scale` | number | CFG scale override |
| `video_stg_guidance_scale` | number | STG scale override |

## Images Array (Explicit Keyframe Mapping)

If you want full control over which image goes to which frame:

```json
{
  "images": [
    { "path": "start_frame.png", "frame_idx": 0, "strength": 0.9 },
    { "path": "shot_01_end.png", "frame_idx": 120, "strength": 0.9 }
  ]
}
```

- `path`: relative to the scene folder
- `frame_idx`: target frame index (0 = first, N-1 = last)
- `strength`: conditioning strength (0.0–1.0, typically 0.9)

If `images` is omitted, the script auto-discovers:
- `start_frame.png` → frame 0
- `shot_01_*.png` → last frame

## Example config.json

```json
{
  "global_prompt": "High-quality 3D animation in the style of Paw Patrol, vibrant colors, clean CGI, cinematic lighting",
  "duration": 5,
  "specific_prompts": [
    "Chase lying on the floor, dizzy. Skye standing next to him, laughing.",
    "Chase lying still and dazed. Skye shaking with laughter, says: \"Case closed!\""
  ],
  "seed": 42,
  "dimensions": { "width": 480, "height": 854 },
  "negative_prompt": "blurry, low quality, distorted",
  "fps": 24,
  "num_inference_steps": 30,
  "description": "Chase dizzy, Skye laughing"
}
```
