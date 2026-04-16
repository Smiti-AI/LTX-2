# Skye LoRA — Experiment Plan

**Goal:** Fine-tune LTX-2.3 on Skye (PAW Patrol) footage to generate character-faithful videos — appearance, movement, and voice — for producing custom birthday message videos.

**Server:** `35.238.2.51` · **Model:** `ltx-2.3-22b-dev.safetensors` (22B params) · **W&B:** [ltx-2-skye-lora project](https://wandb.ai/shayzweig-smiti-ai/ltx-2-skye-lora)

---

## Are We Using the Official Lightricks Training Script?

**Yes.** This is important context. The community tutorial (Ostress) used **AI Toolkit** — a general-purpose third-party training framework. We are using **`packages/ltx-trainer`**, which is the official Lightricks training repository built specifically for LTX-2/2.3. This means:

- The configs, VRAM optimizations, audio-video joint training, and data pipeline are all purpose-built for this model
- The official script correctly handles the 22B-specific `cross_attention_adaln` path (which AI Toolkit may not)
- Results may differ from the tutorial because the training machinery is different

The tutorial is still useful as a reference for **what "good" looks like at various step counts** and for understanding the data preparation philosophy — but it's not an apples-to-apples comparison.

---

## What Are the 48 Transformer Blocks?

Think of the model as a 48-floor assembly line. Raw noise goes in at floor 1; a coherent video comes out at floor 48. Each floor (block) does one complete round of:

1. **Video self-attention** — every frame checks every other frame: *"are we consistent with each other?"*
2. **Video-text attention** — every frame checks the prompt: *"does what I look like match the description?"*
3. **Audio self-attention** — audio segments check each other for temporal coherence
4. **Audio-text attention** — audio checks the prompt: *"does what I sound like match the description?"*
5. **Audio↔Video cross-attention** — video and audio synchronize with each other
6. **Feed-forward processing** — each position independently processes what it learned from the above

The first ~15 blocks establish global structure (is this even a dog? is there a helicopter?). The middle blocks (~15–35) build character identity, motion patterns, and voice character. The last blocks (~35–48) handle fine detail, facial expressions, and audio-visual sync.

LoRA is applied to the routing weights (Q, K, V, output) inside every attention module across all 48 blocks. We're teaching all 48 floors simultaneously to route attention differently when they see Skye-like content.

---

## What We Learned from the Tutorial

**Tutorial setup:**
- 19 clips, ~4–8 seconds each (avg ~5s), single room/scene, one person
- Rank 32, AI Toolkit framework
- Timestep strategy: "high noise" for first 2000 steps → switch to "balanced"
- Results:
  - Step 29: "clearly not me" — nothing meaningful yet
  - Step ~2000: "looking pretty good" — voice nailing it, background correct
  - Step 5000: "That is me" — face, clothing, voice all locked in

**The key metric: clip exposures.** At batch_size=1, each training step uses 1 clip. With 19 clips and 5000 steps, the model sees each clip ~**263 times** before converging.

**What this means for our 409-clip dataset:**

| Steps | Clip exposures per clip | Tutorial equivalent |
|-------|------------------------|---------------------|
| 1,000 | 2.4× | Tutorial step ~45 — too early to judge |
| 2,500 | 6.1× | Tutorial step ~115 — early learning |
| 5,000 | 12.2× | Tutorial step ~230 — starting to look right |
| 10,000 | 24.4× | Tutorial step ~460 — solid character |
| 20,000 | 48.8× | Tutorial step ~930 — approaching tutorial's step 2000 quality |

**Important nuance:** The tutorial was memorizing a single scene. Our 409 clips span 10 seasons with varied backgrounds, other characters, and lighting. More variety = the model generalizes rather than memorizes, so it needs **fewer repetitions per clip** to learn Skye's core identity — but the relationship isn't linear. Working assumption: we need roughly **20–50 repetitions per clip** (8,000–20,000 steps) for strong character fidelity, vs the tutorial's 263 repetitions for a single-scene overfit.

**Loss curve expectations:**

| Phase | Loss range | What it means |
|-------|-----------|---------------|
| Step 0 | ~0.8–1.0 | Base model has no concept of Skye yet |
| Steps 1–500 | Rapid drop | Model learns Skye's basic colors, shape, and helicopter |
| Steps 500–3000 | Slower descent | Voice character, expression style, motion patterns |
| Steps 3000–10000 | Gradual plateau | Fine-grained identity — face details, speech inflections |
| Plateau | ~0.3–0.5 | Good training range |
| Below 0.2 | Overfitting risk | Especially if validation videos start looking identical |

If loss flattens above 0.6 for many steps: underfitting — raise rank or add FFN layers.
If loss drops below 0.2 fast with <5000 steps: likely memorizing, not generalizing — check that data has real variety.

---

## Experiment Plan

### Phase 0 — Full Data Pipeline (prerequisite, run once)

**Step 1: Download all clips** (~20–40 min)
```bash
# Already running — check progress:
tail -f /tmp/ltx_current.log
```
Output: `/home/efrattaig/data/golden_skye_train/dataset.json` + MP4 files

**Step 2: Preprocess — encode all latents** (~3–6 hrs depending on clip count)
```bash
ssh efrattaig@35.238.2.51
cd /home/efrattaig/LTX-2
nohup /home/efrattaig/.local/bin/uv run python packages/ltx-trainer/scripts/process_dataset.py \
  /home/efrattaig/data/golden_skye_train/dataset.json \
  --resolution-buckets "960x544x49;960x544x97" \
  --model-path /home/efrattaig/models/LTX-2.3/ltx-2.3-22b-dev.safetensors \
  --text-encoder-path /home/efrattaig/models/gemma-3-12b-it-qat-q4_0-unquantized \
  --with-audio \
  --output-dir /home/efrattaig/data/golden_skye_preprocessed \
  --load-text-encoder-in-8bit \
  > /tmp/ltx_current.log 2>&1 < /dev/null &
```
Output: `golden_skye_preprocessed/latents/`, `conditions/`, `audio_latents/` — all file counts must match before proceeding.

---

### Phase 1 — First Training Runs (run in parallel on two machines)

Once preprocessing is done, copy preprocessed data to a second server and launch both experiments simultaneously.

**Machine 1: EXP-1 — Baseline (rank 16, 2000 steps)**

*Why:* Quick validation that the character is being learned with the full dataset. Rank 16 = fewer parameters, faster iteration. If this looks good at step 500, we have confidence EXP-2 will work.

*Config:* `packages/ltx-trainer/configs/skye_exp1_baseline.yaml` — update `steps: 2000`

*Decision point:* At step 500 — validation video should show pink Cockapoo in recognizable Skye-like context. If completely generic: check caption quality and audio latent counts.

```bash
# Machine 1
nohup /tmp/launch_train.sh </dev/null >/dev/null 2>&1 &  # (update script to use exp1 config)
```

**Machine 2: EXP-4 — Image-to-Video focus (rank 32, 2500 steps)**

*Why:* This is the production use case — you provide a clean Skye illustration as the first frame and animate it. `first_frame_conditioning_p=0.8` means 80% of training steps practice from a given start frame. This runs independently of EXP-1/2.

*Config:* `packages/ltx-trainer/configs/skye_exp4_i2v.yaml`

*Decision point:* At step 1000 — does the model continue motion naturally from the start frame image? Voice should match.

```bash
# Machine 2
# (same launch pattern, different config)
```

---

### Phase 2 — Main Character LoRA (sequential, after EXP-1 shows learning)

**EXP-2 — Standard (rank 32, 5000 steps)** ← Primary experiment

*Why rank 32:* The tutorial confirmed rank 32 is sufficient for full character+voice capture. Rank 16 (EXP-1) will likely underfit at the fine detail level.

*Why 5000 steps:* With our dataset size (~100–400 clips), 5000 steps gives ~12–50 clip exposures — roughly equivalent to where the tutorial was when it called results "pretty good" (step 2000 with 19 clips = ~105 reps/clip × correction factor for dataset variety). Check W&B at step 2500 and extend if the character is still improving.

*Config:* `packages/ltx-trainer/configs/skye_exp2_standard.yaml` — update `steps: 5000`

*Decision point at step 2500:* If validation video shows clear Skye identity → continue to 5000. If loss is still falling steeply → consider extending to 10000.

```bash
# After EXP-1 confirms learning (step 500+):
# Update config steps to 5000, launch on Machine 1 (after EXP-1 completes or on a third machine)
```

---

### Phase 3 — Conditional (only if EXP-2 underfits)

**EXP-3 — High Capacity (rank 32 + FFN, 3000 steps)**

*When to run:* Only if EXP-2 results at step 2500 show the character looks generic, movements don't match Skye's style (helicopter spin, paw gestures), or face expressions aren't captured.

*What it adds:* Feed-forward layers (`ff.net.0.proj`, `ff.net.2`, `audio_ff.*`) — these store associative memory and are responsible for motion style and behavioral patterns.

*Config:* `packages/ltx-trainer/configs/skye_exp3_highcap.yaml`

---

### Execution Timeline

```
[Now]          Download 409 clips (~30 min)
               ↓
[+30 min]      Preprocess all clips (~4-6 hrs)
               ↓
[+5–7 hrs]     Machine 1: EXP-1 (2000 steps, ~5 hrs)
               Machine 2: EXP-4 (2500 steps, ~6 hrs)  ← parallel
               ↓
[+12 hrs]      Evaluate EXP-1 step 500 validation
               ↓
[If OK]        Machine 1: EXP-2 (5000 steps, ~12 hrs)
               ↓
[+24 hrs]      Evaluate EXP-2 at step 2500
               ↓
[If underfitting] → EXP-3
[If I2V needed]   → EXP-4 already running
```

---

## Evaluation Criteria

At each validation checkpoint, ask:

1. **Appearance** — Is the character pink? Cockapoo breed? Aviator helmet visible?
2. **Motion** — Does she move naturally? Helicopter-style flight patterns?
3. **Voice** — Does the audio sound like a young girl's voice? Skye's pitch and energy?
4. **Prompt adherence** — Does the generated video match what the validation prompt says she's doing?
5. **Loss trend** — Is it still falling? How fast? Compare across runs in W&B.

---

## Key Files

| File | Purpose |
|------|---------|
| `packages/ltx-trainer/scripts/train.py` | **The training entry point — official Lightricks script** |
| `packages/ltx-trainer/scripts/process_dataset.py` | Preprocessing: encodes all latents |
| `packages/ltx-trainer/configs/skye_exp1_baseline.yaml` | Rank 16, 2000 steps |
| `packages/ltx-trainer/configs/skye_exp2_standard.yaml` | Rank 32, 5000 steps ⭐ primary |
| `packages/ltx-trainer/configs/skye_exp3_highcap.yaml` | Rank 32 + FFN (contingency) |
| `packages/ltx-trainer/configs/skye_exp4_i2v.yaml` | Rank 32, I2V focused |
| `LTX_2.3_LoRA_Training_Analysis.md` | Architecture deep-dive with plain-language explanations |

---

## Current Status

- ✅ Dry run (10 clips, 100 steps): pipeline confirmed working end-to-end
- ✅ FFmpeg installed on server (required for audio extraction)
- ✅ W&B authenticated and logging validation videos
- ✅ Start frame (`skye_helicopter_birthday_gili.png`) on server, wired into validation
- ✅ `process_captions.py` bug fixed (absolute path handling)
- 🔄 **Full dataset download in progress** (`/tmp/ltx_current.log`)
- ⬜ Full preprocessing
- ⬜ EXP-1 + EXP-4 (parallel)
- ⬜ EXP-2 (main run)