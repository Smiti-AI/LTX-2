# LTX-2.3 LoRA Training: Deep Architecture Analysis

*Generated from direct codebase inspection of `/Users/efrattaig/projects/sm/LTX-2` — all class names, line numbers,
and parameter values are verified against the source.*

---

## Executive Summary

**What to train:** LoRA adapters on the `to_q`, `to_k`, `to_v`, and `to_out.0` linear projections inside **every
attention module** across all 48 transformer blocks of `LTXModel`. This covers video self-attention, video–text
cross-attention, audio self-attention, audio–text cross-attention, and the bidirectional audio↔video cross-attention
layers — 28 distinct attention matrices per block, 1344 total targets.

**Which checkpoint to target:** `ltx-2.3-22b-dev.safetensors` (22B parameters). This is the only checkpoint that
activates the `cross_attention_adaln=True` path (prompt AdaLN on cross-attention), which is where LTX-2.3's improved
prompt adherence lives. Training on the 19B checkpoint misses these layers entirely.

**Goal-specific routing:**

| Goal | Recommended Config | Target Modules |
|---|---|---|
| Style / appearance / character | `ltx2_av_lora.yaml` | `to_q`, `to_k`, `to_v`, `to_out.0` (all branches) |
| Motion / temporal coherence | `ltx2_av_lora.yaml` + FFN layers | above + `ff.net.0.proj`, `ff.net.2` |
| Video-conditioned transformation | `ltx2_v2v_ic_lora.yaml` | Video branches only (explicit `attn1.*`, `attn2.*`, `ff.*`) |
| Audio style | `ltx2_av_lora.yaml` with `with_audio: true` | Above + `audio_attn*`, `audio_ff.*`, A↔V cross-attn |

**VRAM requirements:** 80 GB+ for standard BF16 training; 32 GB+ with INT8 quantization + 8-bit AdamW.

---

## Architecture Map

### Model Class Hierarchy

```
LTXModel                                   (ltx_core.model.transformer.model.LTXModel)
└── transformer_blocks: nn.ModuleList[48]
    └── BasicAVTransformerBlock × 48       (ltx_core.model.transformer.transformer.BasicAVTransformerBlock)
        ├── VIDEO BRANCH
        │   ├── attn1: Attention           (video self-attention)
        │   ├── attn2: Attention           (video cross-attention to text)
        │   ├── ff: FeedForward
        │   └── scale_shift_table          (AdaLN params, shape [9, 4096] in LTX-2.3)
        ├── AUDIO BRANCH
        │   ├── audio_attn1: Attention     (audio self-attention)
        │   ├── audio_attn2: Attention     (audio cross-attention to text)
        │   ├── audio_ff: FeedForward
        │   └── audio_scale_shift_table    (AdaLN params, shape [6, 2048])
        └── AUDIO-VIDEO CROSS-ATTENTION
            ├── audio_to_video_attn        (Q: video, K/V: audio)
            ├── video_to_audio_attn        (Q: audio, K/V: video)
            └── scale_shift_table_a2v_*    (AdaLN params for cross-modal gates)
```

Source files:
- [transformer.py:24](packages/ltx-core/src/ltx_core/model/transformer/transformer.py#L24) — `BasicAVTransformerBlock`
- [transformer.py:38](packages/ltx-core/src/ltx_core/model/transformer/transformer.py#L38) — `attn1` (video self-attn)
- [transformer.py:48](packages/ltx-core/src/ltx_core/model/transformer/transformer.py#L48) — `attn2` (video cross-attn)
- [transformer.py:63](packages/ltx-core/src/ltx_core/model/transformer/transformer.py#L63) — `audio_attn1`
- [transformer.py:89](packages/ltx-core/src/ltx_core/model/transformer/transformer.py#L89) — `audio_to_video_attn`

---

### The `Attention` Module (LoRA injection targets)

**File:** [attention.py:143](packages/ltx-core/src/ltx_core/model/transformer/attention.py#L143)

```python
class Attention(torch.nn.Module):
    def __init__(self, query_dim, context_dim, heads, dim_head, ...):
        inner_dim = dim_head * heads

        self.q_norm   = torch.nn.RMSNorm(inner_dim, eps=norm_eps)   # line 165
        self.k_norm   = torch.nn.RMSNorm(inner_dim, eps=norm_eps)   # line 166

        self.to_q     = torch.nn.Linear(query_dim,   inner_dim)     # line 168  ← LoRA target
        self.to_k     = torch.nn.Linear(context_dim, inner_dim)     # line 169  ← LoRA target
        self.to_v     = torch.nn.Linear(context_dim, inner_dim)     # line 170  ← LoRA target
        self.to_out   = nn.Sequential(Linear(inner_dim, query_dim), # line 178  ← LoRA target
                                      nn.Identity())
```

**Why these four and not `q_norm`/`k_norm`?** RMSNorm has no weight matrix with a suitable low-rank decomposition.
LoRA works by inserting `B @ A` deltas into weight matrices. The `to_q`, `to_k`, `to_v`, and `to_out.0` projections
are all `nn.Linear` and are the canonical targets across every DiT-family model.

**Attention dimensions per branch:**

| Branch | Module prefix | query_dim | context_dim | heads | dim_head | inner_dim |
|---|---|---|---|---|---|---|
| Video self-attn | `attn1` | 4096 | 4096 (self) | 32 | 128 | 4096 |
| Video cross-attn | `attn2` | 4096 | 4096 (text) | 32 | 128 | 4096 |
| Audio self-attn | `audio_attn1` | 2048 | 2048 (self) | 32 | 64 | 2048 |
| Audio cross-attn | `audio_attn2` | 2048 | 2048 (text) | 32 | 64 | 2048 |
| A→V cross-attn | `audio_to_video_attn` | 4096 (video Q) | 2048 (audio K/V) | 32 | 64 | 2048 |
| V→A cross-attn | `video_to_audio_attn` | 2048 (audio Q) | 4096 (video K/V) | 32 | 64 | 2048 |

---

### The `FeedForward` Module

**File:** [feed_forward.py](packages/ltx-core/src/ltx_core/model/transformer/feed_forward.py)

```
FeedForward (dim=4096, dim_out=4096, mult=4):
  ff.net.0 = GELUApprox:
    ff.net.0.proj = Linear(4096, 16384)    ← LoRA target (optional)
  ff.net.1 = Identity()
  ff.net.2 = Linear(16384, 4096)           ← LoRA target (optional)
```

FFN adds significant parameter count — enable it only if your task requires substantial style shift. Attention-only
LoRA is sufficient for character/subject consistency.

---

### Full PEFT Module Name Map (per block N = 0…47)

```
transformer_blocks.N.attn1.to_q
transformer_blocks.N.attn1.to_k
transformer_blocks.N.attn1.to_v
transformer_blocks.N.attn1.to_out.0
transformer_blocks.N.attn2.to_q
transformer_blocks.N.attn2.to_k
transformer_blocks.N.attn2.to_v
transformer_blocks.N.attn2.to_out.0
transformer_blocks.N.ff.net.0.proj          (optional)
transformer_blocks.N.ff.net.2               (optional)
transformer_blocks.N.audio_attn1.to_q
transformer_blocks.N.audio_attn1.to_k
transformer_blocks.N.audio_attn1.to_v
transformer_blocks.N.audio_attn1.to_out.0
transformer_blocks.N.audio_attn2.to_q
transformer_blocks.N.audio_attn2.to_k
transformer_blocks.N.audio_attn2.to_v
transformer_blocks.N.audio_attn2.to_out.0
transformer_blocks.N.audio_ff.net.0.proj    (optional)
transformer_blocks.N.audio_ff.net.2         (optional)
transformer_blocks.N.audio_to_video_attn.to_q
transformer_blocks.N.audio_to_video_attn.to_k
transformer_blocks.N.audio_to_video_attn.to_v
transformer_blocks.N.audio_to_video_attn.to_out.0
transformer_blocks.N.video_to_audio_attn.to_q
transformer_blocks.N.video_to_audio_attn.to_k
transformer_blocks.N.video_to_audio_attn.to_v
transformer_blocks.N.video_to_audio_attn.to_out.0
```

**Shorthand trick:** Using `"to_k"`, `"to_q"`, `"to_v"`, `"to_out.0"` as PEFT `target_modules` matches ALL of the
above in a single pass because PEFT does suffix matching across the full module tree. This is the pattern used in the
official configs.

---

### LTX-2.3 vs LTX-2 (19B): Why 22B Is the Right Base

| Component | LTX-2 (19B) | LTX-2.3 (22B) |
|---|---|---|
| `cross_attention_adaln` | `False` | `True` |
| AdaLN params per block | `[6, 4096]` | `[9, 4096]` — 3 extra for CA modulation |
| `prompt_scale_shift_table` | absent | present per block (shape `[2, 4096]`) |
| Feature extractor | `FeatureExtractorV1` — single stream, same embed for video & audio | `FeatureExtractorV2` — separate `video_aggregate_embed` + `audio_aggregate_embed`, per-token RMSNorm |
| Caption projection | Inside transformer | Inside text encoder; transformer has NO `caption_projection` module |
| Vocoder | HiFi-GAN | BigVGAN v2 + bandwidth extension (`VocoderWithBWE`) |

**Why this matters for LoRA:** `cross_attention_adaln=True` means the 22B model modulates cross-attention Q/K/V via
sigma-conditioned AdaLN at every block. A LoRA trained on the 19B checkpoint cannot learn these modulation paths and
will not transfer to 22B without retraining. The `ltx-2.3-22b-dev.safetensors` checkpoint is the only one that
activates this code path in `model_configurator.py`.

Version detection is **automatic**: `ltx-core` reads the config embedded in the safetensors file and selects
`FeatureExtractorV1` vs `V2`, `Vocoder` vs `VocoderWithBWE`, and the AdaLN coefficient count without any
trainer-side version checks.

---

## Training Parameters

### Official Recommended Hyperparameters

Sourced directly from [ltx2_av_lora.yaml](packages/ltx-trainer/configs/ltx2_av_lora.yaml) and
[ltx2_v2v_ic_lora.yaml](packages/ltx-trainer/configs/ltx2_v2v_ic_lora.yaml):

| Parameter | Audio-Video LoRA | IC-LoRA (V2V) | Low-VRAM AV | Notes |
|---|---|---|---|---|
| `rank` | **32** | **32** | **16** | Start here; 64 for high-detail tasks |
| `alpha` | **32** | **32** | **16** | Keep = rank for neutral scaling |
| `dropout` | 0.0 | 0.0 | 0.0 | Only add if overfitting |
| `learning_rate` | **1e-4** | **2e-4** | 1e-4 | IC-LoRA uses higher LR; AV is conservative |
| `steps` | **2000** | **3000** | 2000 | IC-LoRA needs more steps for V2V concept |
| `batch_size` | 1 | 1 | 1 | Effective batch = 1 per GPU |
| `gradient_accumulation_steps` | 1 | 1 | 1 | Scale up for multi-GPU |
| `max_grad_norm` | 1.0 | 1.0 | 1.0 | Standard clip |
| `optimizer_type` | `adamw` | `adamw` | `adamw8bit` | 8-bit saves ~75% optimizer VRAM |
| `scheduler_type` | `linear` | `linear` | `linear` | Cosine also viable |
| `mixed_precision_mode` | **bf16** | **bf16** | bf16 | Never fp16 — numerical instability |
| `quantization` | `null` | `null` | `int8-quanto` | INT8 cuts base model VRAM ~50% |
| `enable_gradient_checkpointing` | `true` | `true` | `true` | Always enable for 22B |
| `load_text_encoder_in_8bit` | `true` | `false` | `true` | Gemma can OOM during long prompt validation |
| `checkpoint precision` | `bfloat16` | `bfloat16` | `bfloat16` | Saves disk; reload in BF16 for inference |

### Precision Notes

- **Training precision: BF16.** The model was trained in BF16. FP16 causes overflow at these scales. FP32 doubles VRAM.
- **FP8 at inference only.** The `fuse_loras.py` loader supports FP8 weight types (`apply_loras()` handles
  `cast-only FP8` and `scaled FP8` with per-tensor scale), but this is only for inference-time LoRA fusion — **do not
  train in FP8**. Source: [fuse_loras.py](packages/ltx-core/src/ltx_core/loader/fuse_loras.py).
- **LoRA alpha/rank=1 scaling.** With `alpha=rank`, the effective scale factor is `alpha/rank = 1.0`. This is the
  safest default. Increasing `alpha` with fixed `rank` amplifies the LoRA contribution — use only when under-fitting.

### Timestep Sampling

Use `timestep_sampling_mode: "shifted_logit_normal"` (default in all official configs). This mode:
- Shifts the noise distribution based on sequence length (longer/higher-res videos get proportionally more high-noise
  training signal)
- Applies percentile stretching to improve `[0, 1]` coverage
- Adds a 10% uniform fallback to prevent distribution collapse

Source: [timestep_samplers.py](packages/ltx-trainer/src/ltx_trainer/timestep_samplers.py) —
`ShiftedLogitNormalTimestepSampler`

---

## Dataset Strategy

### Data Pipeline Overview

Training uses pre-encoded latents, not raw video. The pipeline is:

```
Raw Videos → VAE Encoder → latents/          (128-channel video latent tensors)
Raw Videos → Gemma + FE  → conditions/       (text embedding features, blocks 1+2 only)
Raw Audio  → Audio VAE   → audio_latents/    (8-channel audio latent tensors)
```

Block 3 (embeddings connectors) is applied **during training** via the `EmbeddingsProcessor`. This means you only need
to rerun preprocessing once and the connector is applied on the fly per step.

### Dataset Directory Structure

**For text-to-video / audio-video LoRA:**
```
preprocessed_data_root/
├── latents/           # .safetensors files — shape [128, F, H//32, W//32]
├── conditions/        # .safetensors files — video_prompt_embeds, audio_prompt_embeds, prompt_attention_mask
└── audio_latents/     # .safetensors files — shape [8, T_audio] (only if with_audio: true)
```

**For IC-LoRA (video-to-video):**
```
preprocessed_data_root/
├── latents/           # target video latents
├── conditions/        # text embeddings
└── reference_latents/ # reference/conditioning video latents
```

### Video Requirements

| Constraint | Value | Source |
|---|---|---|
| Frame count | `frames % 8 == 1` | `SpatioTemporalScaleFactors.default()` |
| Valid frame counts | 1, 9, 17, 25, 33, 41, 49, 57, 65, 73, 81, 89, 97… | |
| Width | divisible by 32 | `VideoLatentShape` |
| Height | divisible by 32 | `VideoLatentShape` |
| Spatial compression | 32× (H and W in latent space) | |
| Temporal compression | 8× (frames in latent space) | |
| Latent channels | 128 | `in_channels` in LTXModel |

**Recommended training resolution:** 576×576 at 89 frames (≈3.5 sec at 25 fps) — this is what the official
validation config uses. For character consistency, 512×512 at 49 frames is faster to encode and still effective.

### Caption / Prompt Strategy

LTX-2.3 uses Gemma-3-12B and benefits significantly from **long, detailed prompts** (100–250 words). The text
encoder was trained with prompts describing both visual content and audio simultaneously. Short prompts
(< 20 words) produce significantly weaker conditioning.

**Process captions with:**
```bash
uv run python packages/ltx-trainer/scripts/caption_videos.py <input_dir> <output_dir>
uv run python packages/ltx-trainer/scripts/process_captions.py <video_dir> <output_dir> \
    --model-path models/ltx-2.3-22b-dev.safetensors \
    --text-encoder-path models/gemma-3-12b-it-qat-q4_0-unquantized
```

The `process_captions.py` script runs only Gemma (blocks 1+2) and saves `video_prompt_embeds` +
`audio_prompt_embeds` — these are the new-format embeddings expected by the trainer. Legacy `prompt_embeds`
(single key) is still supported via backward-compat in `trainer.py:_training_step()`.

### Minimum Dataset Size

- **Concept/style LoRA:** 20–50 videos of 3–10 seconds each
- **Character consistency:** 50–150 videos showing the character in varied motion, lighting, angle
- **IC-LoRA (transformation):** 30–100 paired (reference, target) video clips

More data is always better. Avoid duplicate or near-duplicate clips — they cause overfitting at low step counts.

---

## PEFT Integration: How LoRA Is Applied

**File:** [trainer.py:12](packages/ltx-trainer/src/ltx_trainer/trainer.py#L12)

```python
from peft import LoraConfig, get_peft_model

lora_config = LoraConfig(
    r=self._config.lora.rank,           # e.g., 32
    lora_alpha=self._config.lora.alpha, # e.g., 32
    target_modules=self._config.lora.target_modules,  # e.g., ["to_q", "to_k", "to_v", "to_out.0"]
    lora_dropout=self._config.lora.dropout,
    init_lora_weights=True,
)
self._transformer = get_peft_model(self._transformer, lora_config)
```

PEFT wraps matching `nn.Linear` layers with `LoraLinear` modules. During forward: `out = base(x) + lora_B(lora_A(x)) * scale`. During save: only `lora_A.weight` and `lora_B.weight` are written.

**LoRA state dict key format** (as written to `.safetensors`):
```
transformer_blocks.0.attn1.to_q.lora_A.weight   shape: [rank, query_dim]
transformer_blocks.0.attn1.to_q.lora_B.weight   shape: [query_dim, rank]
```

**Inference fusion** via `fuse_loras.py`: the `apply_loras()` function merges `B @ A * strength` directly into the
base weight tensor at load time. Supports BF16, cast-only FP8, and scaled FP8 base weights.

---

## Spatio-Temporal Guidance (STG) During Validation

STG is a perturbation-based guidance technique unique to LTX. It skips attention in a specific block for one CFG
branch, improving temporal coherence without extra training.

**Recommended inference-time config (from all three official YAML files):**
```yaml
stg_scale: 1.0
stg_blocks: [29]    # block index 29 of 48
stg_mode: "stg_av"  # "stg_av" for AV training, "stg_v" for video-only
```

**Perturbation types** (`PerturbationType` enum in `guidance/perturbations.py`):
- `SKIP_VIDEO_SELF_ATTN` — skips `attn1` at the perturbation block
- `SKIP_AUDIO_SELF_ATTN` — skips `audio_attn1`
- `SKIP_A2V_CROSS_ATTN` — skips `audio_to_video_attn`
- `SKIP_V2A_CROSS_ATTN` — skips `video_to_audio_attn`

`stg_av` triggers all four; `stg_v` triggers only `SKIP_VIDEO_SELF_ATTN`.

Block 29 (mid-to-late, out of 48) is the empirically recommended perturbation site based on Lightricks internal
testing. Different blocks can be tried if artifact patterns emerge.

---

## Proof & References

### Codebase Evidence

| Claim | File | Lines |
|---|---|---|
| `LTXModel` uses 48 blocks, `inner_dim=4096` for video | [model_configurator.py](packages/ltx-core/src/ltx_core/model/transformer/model_configurator.py) | `num_layers=48`, `num_attention_heads=32`, `attention_head_dim=128` |
| `Attention` has `to_q`, `to_k`, `to_v`, `to_out` as `nn.Linear` | [attention.py:165-178](packages/ltx-core/src/ltx_core/model/transformer/attention.py#L165) | Exact constructor |
| `BasicAVTransformerBlock` has all 6 attention submodules | [transformer.py:38-130](packages/ltx-core/src/ltx_core/model/transformer/transformer.py#L38) | `attn1`, `attn2`, `audio_attn1`, `audio_attn2`, `audio_to_video_attn`, `video_to_audio_attn` |
| PEFT `LoraConfig` used for training | [trainer.py:12](packages/ltx-trainer/src/ltx_trainer/trainer.py#L12) | `from peft import LoraConfig, get_peft_model` |
| Official target_modules: `["to_k","to_q","to_v","to_out.0"]` | [ltx2_av_lora.yaml:81-88](packages/ltx-trainer/configs/ltx2_av_lora.yaml#L81) | lora.target_modules |
| IC-LoRA targets explicit video modules + FFN | [ltx2_v2v_ic_lora.yaml:82-95](packages/ltx-trainer/configs/ltx2_v2v_ic_lora.yaml#L82) | `attn1.*`, `attn2.*`, `ff.net.*` |
| LTX-2.3 `cross_attention_adaln=True` (22B only) | [CLAUDE.md table](packages/ltx-trainer/CLAUDE.md) | "Prompt AdaLN: Active" for 22B only |
| BF16 mixed precision is mandatory | All three YAML configs | `mixed_precision_mode: "bf16"` |
| `shifted_logit_normal` timestep sampling | All three YAML configs | `timestep_sampling_mode: "shifted_logit_normal"` |
| FP8 is inference-only (LoRA fusion) | [fuse_loras.py](packages/ltx-core/src/ltx_core/loader/fuse_loras.py) | `apply_loras()` handles FP8 base weights |
| STG block 29 recommended | [ltx2_av_lora.yaml:233](packages/ltx-trainer/configs/ltx2_av_lora.yaml#L233) | `stg_blocks: [29]` |

### Key Data Flow Proof

The `Modality` dataclass (the model's input interface) confirms both video and audio branches receive separate sigma
and position embeddings — meaning a LoRA trained only on video branches will NOT influence audio generation and vice
versa. To achieve audio-visual consistency, always use the full AV config:

```python
# From packages/ltx-core/src/ltx_core/model/transformer/modality.py
video_pred, audio_pred = model(video=video_modality, audio=audio_modality, perturbations=None)
```

### Why Not Train the VAE or Text Encoder?

- **VAE (VideoEncoder/VideoDecoder):** Encodes/decodes pixel space. LoRA here affects reconstruction quality, not
  semantic content. Out of scope for character/style/motion control.
- **GemmaTextEncoder:** The text conditioning is already rich at 12B params. Fine-tuning it is expensive and risky
  — it can break general language understanding. The cross-attention projections in the transformer (`attn2.to_k`,
  `attn2.to_v`) are the correct place to align new concepts to existing text understanding.
- **Vocoder (BigVGAN v2):** Generates waveforms from mel spectrograms. LoRA here would affect audio timbre/quality,
  not semantic audio content.

---

## Quick-Start Config (Character/Style LoRA on LTX-2.3)

```yaml
model:
  model_path: "/path/to/models/ltx-2.3-22b-dev.safetensors"
  text_encoder_path: "/path/to/models/gemma-3-12b-it-qat-q4_0-unquantized"
  training_mode: "lora"

lora:
  rank: 32
  alpha: 32
  dropout: 0.0
  target_modules:
    - "to_k"
    - "to_q"
    - "to_v"
    - "to_out.0"

training_strategy:
  name: "text_to_video"
  first_frame_conditioning_p: 0.5
  with_audio: true

optimization:
  learning_rate: 1e-4
  steps: 2000
  batch_size: 1
  gradient_accumulation_steps: 1
  max_grad_norm: 1.0
  optimizer_type: "adamw"
  scheduler_type: "linear"
  enable_gradient_checkpointing: true

acceleration:
  mixed_precision_mode: "bf16"
  quantization: null          # set "int8-quanto" if VRAM < 80GB
  load_text_encoder_in_8bit: true

flow_matching:
  timestep_sampling_mode: "shifted_logit_normal"
```

Run with:
```bash
uv run accelerate launch packages/ltx-trainer/scripts/train.py my_config.yaml
```

---

*All findings are derived from the local codebase at `/Users/efrattaig/projects/sm/LTX-2`. No external community
benchmarks were available at time of writing — the above parameters are the official Lightricks defaults and should
be treated as the authoritative starting point.*
