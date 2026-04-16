# Skye LoRA — Experiment Plan v1

**Project:** Fine-tune LTX-2.3 (22B audio-video model) on Skye PAW Patrol footage to generate character-faithful birthday videos — correct appearance, voice, and motion — from a reference start frame.

**W&B project:** [ltx-2-skye-lora](https://wandb.ai/shayzweig-smiti-ai/ltx-2-skye-lora)
**Server:** `35.238.2.51` (efrattaig)

---

## Goals

### Short-Term (next 2–3 weeks)
**Produce one good birthday message video.**

Given the start frame `skye_helicopter_birthday_gili/start_frame.png`, generate a 6–15 second video where Skye:
- Looks unmistakably like Skye — pink Cockapoo, aviator helmet, helicopter cockpit
- Speaks in a young girl's cheerful voice with Skye's energy
- Has mouth movement that matches the speech
- Moves smoothly with no flicker or jitter

This is the `run_skye_like_api.py` workflow: you provide a start frame, a prompt with the birthday message, and the LoRA checkpoint — you get a video.

### Long-Term
**A reliable production pipeline.** Given any child's name and a short message, generate a 10–15 second Skye video on demand. This requires:
- A LoRA checkpoint that generalizes to novel prompts and start frames (not just memorized training scenes)
- A working `run_skye_like_api.py --lora <checkpoint>` command
- A library of start frames for different "scenes" (cockpit, outdoor flight, Lookout tower, portrait)
- Optional: Phase 2 data work — split long clips to align training audio with dialogue, for sharper voice fidelity

---

## Which Scripts We Use

**We use the official Lightricks training scripts from `packages/ltx-trainer/`.** We did not write any custom training code. Here's exactly what runs what:

| Script | What it does | Source |
|--------|-------------|--------|
| [`packages/ltx-trainer/scripts/train.py`](packages/ltx-trainer/scripts/train.py) | Main training loop | Official Lightricks |
| [`packages/ltx-trainer/scripts/process_dataset.py`](packages/ltx-trainer/scripts/process_dataset.py) | Encodes all videos + audio + text captions into latents (runs once, before training) | Official Lightricks |
| [`packages/ltx-trainer/configs/skye_exp*.yaml`](packages/ltx-trainer/configs/) | Training parameters for each experiment | **Written by us**, derived from official `ltx2_av_lora.yaml` |
| [`run_skye_like_api.py`](run_skye_like_api.py) | Full HQ inference for evaluation — Stage 1 (22B at half-res) + Stage 2 (2× upsampler) | Exists in repo, we added `--lora` flag |

The community Ostress tutorial used AI Toolkit (a third-party framework). We use the Lightricks-native trainer — purpose-built for LTX-2.3 and handles audio-video joint training correctly.

---

## The Data

- **111 clips** from `gs://video_gen_dataset/TinyStories/data_sets/golden_skye/`
- Skye-only, all PAW Patrol seasons, average ~17.6s per clip
- Each clip was center-cropped to 2–4s during preprocessing (the model trains on short segments)
- Each clip has: Skye's exact dialogue prepended to a detailed scene description as the caption
- Preview video (111 clips with subtitles): `output/skye_preview.mp4` — watch this to review data quality

### Caption format (what the model trains on)
```
SKYE says "Where are you going, silly goose?". A fixed medium close-up frames Skye,
a golden-brown Cockapoo pup with a pink aviator helmet, in her helicopter cockpit...
```
The "SKYE says..." trigger word + transcript + visual description together teach the model what Skye looks like, what she sounds like, and what she says.

---

## Experiment Configs — Full Details

All configs live in [`packages/ltx-trainer/configs/`](packages/ltx-trainer/configs/).

### Shared parameters across all experiments

```yaml
model:
  model_path: "/home/efrattaig/models/LTX-2.3/ltx-2.3-22b-dev.safetensors"
  text_encoder_path: "/home/efrattaig/models/gemma-3-12b-it-qat-q4_0-unquantized"
  training_mode: "lora"

data:
  preprocessed_data_root: "/home/efrattaig/data/golden_skye_preprocessed"

optimization:
  batch_size: 1
  learning_rate: 1e-4
  enable_gradient_checkpointing: true

acceleration:
  mixed_precision_mode: "bf16"
  load_text_encoder_in_8bit: true

checkpoints:
  interval: 250     # Save checkpoint every 250 steps
  keep_last_n: 3    # Keep last 3 checkpoints on disk
```

### Validation prompts (same for all experiments)

Three scenes to track progress across different situations:
1. Skye in helicopter cockpit ("PAW Patrol, on a roll!")
2. Skye doing an aerial barrel roll ("This pup's gotta fly!")
3. Skye at the Lookout tower beside Chase ("Don't worry, I've got this!")

---

### EXP-1 — Baseline · Fast Validation
**Config:** [`packages/ltx-trainer/configs/skye_exp1_baseline.yaml`](packages/ltx-trainer/configs/skye_exp1_baseline.yaml)

| Parameter | Value | Why |
|-----------|-------|-----|
| rank | 16 | Smaller/faster — just need to confirm data pipeline works |
| alpha | 16 | alpha = rank → LoRA strength scale = 1.0 |
| target_modules | `to_q, to_k, to_v, to_out.0` | Attention only |
| steps | 1000 | ~9 exposures per clip; enough to see if Skye is being learned |
| validation interval | every 100 steps | Frequent feedback for go/no-go decision |

**Run command:**
```bash
ssh efrattaig@35.238.2.51
cd /home/efrattaig/LTX-2
nohup /home/efrattaig/.local/bin/uv run python packages/ltx-trainer/scripts/train.py \
  packages/ltx-trainer/configs/skye_exp1_baseline.yaml \
  2>&1 | tee /tmp/ltx_current.log &
```

**Decision gate at step 500:** Does the validation video show a pink dog in a cockpit? If yes → launch EXP-2 immediately (don't wait for EXP-1 to finish). If no → check that all 111 `audio_latents/` files exist.

---

### EXP-2 — Standard · Primary Experiment ⭐
**Config:** [`packages/ltx-trainer/configs/skye_exp2_standard.yaml`](packages/ltx-trainer/configs/skye_exp2_standard.yaml)

| Parameter | Value | Why |
|-----------|-------|-----|
| rank | 32 | Standard for character LoRA — 2× capacity of rank 16 |
| alpha | 32 | alpha = rank |
| target_modules | `to_q, to_k, to_v, to_out.0` | Attention only |
| steps | 5000 | ~45 exposures per clip; tutorial needed ~263 reps with 19 clips for "excellent" — we have 111 clips so 45 reps is reasonable |
| validation interval | every 250 steps | W&B videos to track progression |

**Step justification vs. the tutorial:**
- Tutorial: 19 clips × 5000 steps = ~263 exposures per clip for excellent results
- Us: 111 clips × 5000 steps = ~45 exposures per clip
- Our dataset is 6× larger and more varied (10 seasons, many scenes), so we need fewer reps per clip to generalize without memorizing. 45 reps is the starting target; extend to 10,000 if loss is still falling at step 5000.

**Run command (start after EXP-1 step 500 confirms learning):**
```bash
ssh efrattaig@35.238.2.51
cd /home/efrattaig/LTX-2
nohup /home/efrattaig/.local/bin/uv run python packages/ltx-trainer/scripts/train.py \
  packages/ltx-trainer/configs/skye_exp2_standard.yaml \
  2>&1 | tee /tmp/ltx_current.log &
```

**Checkpoints to evaluate with `run_skye_like_api.py`:** 1000, 2500, 5000

---

### EXP-3 — High Capacity · Contingency Only
**Config:** [`packages/ltx-trainer/configs/skye_exp3_highcap.yaml`](packages/ltx-trainer/configs/skye_exp3_highcap.yaml)

| Parameter | Value | Why |
|-----------|-------|-----|
| rank | 32 | Same as EXP-2 |
| target_modules | attention + **`ff.net.0.proj`, `ff.net.2`, `audio_ff.net.0.proj`, `audio_ff.net.2`** | Adds feed-forward layers (the "motion memory" of each block) |
| steps | 3000 | Shorter — more parameters means faster learning and higher overfitting risk |

**When to run:** ONLY if EXP-2 at step 2500 looks generic — character right but motion style or expressions are wrong. Do not run in parallel with EXP-2. Evaluate EXP-2 first.

**Why the FFN layers help motion:** Attention layers handle "who talks to whom" (spatial/temporal routing). Feed-forward layers handle "what do I remember about this kind of input" — they store associations like "when Skye tilts her head, her ear flops like this." If EXP-2 has the right colors but the wrong movement style, FFN LoRA is the lever.

---

### EXP-4 — Image-to-Video · Production Workflow
**Config:** [`packages/ltx-trainer/configs/skye_exp4_i2v.yaml`](packages/ltx-trainer/configs/skye_exp4_i2v.yaml)

| Parameter | Value | Why |
|-----------|-------|-----|
| rank | 32 | Standard |
| steps | 2500 | Shorter than EXP-2 — this trains one specific skill (start-frame conditioning) |
| **`first_frame_conditioning_p`** | **0.8** | ← **The key difference**: 80% of training steps condition on a clean first frame. This directly trains the model for the birthday video workflow: you give it Skye's face, it animates her. |

EXP-2 uses `first_frame_conditioning_p: 0.5` — half the steps practice free-form generation, half use a conditioning frame. EXP-4 pushes this to 80% so the model becomes much better at faithfully continuing from a given starting pose.

**Run on Machine 2 in parallel with EXP-2.**

**Decision at step 1000:** Does the validation video continue motion naturally from the start frame? Does she move in a way that's consistent with the pose she started in?

---

## Parallel Execution Plan

```
Machine 1:  [Preprocessing ~4h] → [EXP-1, 1000 steps ~2h] → [EXP-2, 5000 steps ~10h]
Machine 2:  [Copy preprocessed data ~30min] → [EXP-4, 2500 steps ~5h]
```

EXP-3 runs on Machine 1 after EXP-2, only if needed.

**Setting up Machine 2:**
```bash
# On Machine 1 — copy preprocessed latents to Machine 2
rsync -avz /home/efrattaig/data/golden_skye_preprocessed/ user@MACHINE2:/data/golden_skye_preprocessed/

# On Machine 2 — pull configs and launch
cd /home/efrattaig/LTX-2 && git pull
nohup /home/efrattaig/.local/bin/uv run python packages/ltx-trainer/scripts/train.py \
  packages/ltx-trainer/configs/skye_exp4_i2v.yaml \
  2>&1 | tee /tmp/ltx_current.log &
```

---

## How to Tell Which Training Is Better

Use three signals together. No single one is enough.

### 1. Loss Curve (W&B, during training)

Watch `train/loss` in the [W&B project](https://wandb.ai/shayzweig-smiti-ai/ltx-2-skye-lora).

**Expected shape:**
- Steps 1–500: Loss drops fast (0.8–1.0 → ~0.5). Model learns basic colors, rough shapes, that this is a pink animal.
- Steps 500–2500: Slower descent. Fine identity details — face, ears, voice character, flight gear.
- Steps 2500+: Plateau. Fine-grained motion, expression nuances.

**Good plateau: 0.3–0.5.** Below 0.2 = overfitting (memorized scenes, won't generalize). Stuck above 0.6 at step 1000 = underfitting (captions may be wrong, or audio latents missing).

**Tutorial reference:** Their dry run loss went from ~0.72 at step 0 to plateau around 0.35–0.45 at full training. Our dry run ended at 0.72 — expected for 100 steps with 10 clips.

Compare EXP-1, EXP-2, EXP-4 loss curves overlaid in W&B. The one that descends smoothest and plateaus lowest (without going below 0.2) wins.

### 2. W&B Validation Videos (every 250 steps, automatic)

These run the fast single-stage pipeline during training. Use them to track the **trend**, not judge final quality.

**Questions at each checkpoint:**
| Step | Question to ask |
|------|----------------|
| 500 | Can I tell this is a pink dog? Is the helicopter cockpit there? |
| 1000 | Does she look specifically like Skye vs. a generic cartoon dog? (ear shape, helmet, proportions) |
| 2000 | Does the voice character sound right? Does she move naturally? |
| 3000+ | Does it look and sound like Skye from the show? |

Compare the same prompt across EXP-1, EXP-2, EXP-4 checkpoints in W&B. You can see all experiments side-by-side since they're in the same project.

### 3. HQ Evaluation with `run_skye_like_api.py` (at key checkpoints)

This runs the full 2-stage pipeline — Stage 1 at 22B, Stage 2 2× upsampler — the same quality as production. Run it at steps 1000, 2500, 5000:

```bash
# Baseline (no LoRA) — run once and keep as reference
uv run python run_skye_like_api.py

# After EXP-2 step 1000:
uv run python run_skye_like_api.py \
  --lora /home/efrattaig/LTX-2/outputs/skye_exp2_standard/checkpoint-1000/pytorch_lora_weights.safetensors

# After EXP-2 step 2500:
uv run python run_skye_like_api.py \
  --lora /home/efrattaig/LTX-2/outputs/skye_exp2_standard/checkpoint-2500/pytorch_lora_weights.safetensors

# After EXP-2 step 5000:
uv run python run_skye_like_api.py \
  --lora /home/efrattaig/LTX-2/outputs/skye_exp2_standard/checkpoint-5000/pytorch_lora_weights.safetensors

# After EXP-4 step 2500 (I2V comparison):
uv run python run_skye_like_api.py \
  --lora /home/efrattaig/LTX-2/outputs/skye_exp4_i2v/checkpoint-2500/pytorch_lora_weights.safetensors
```

Output files are named with `_base.mp4`, `_lora-checkpoint-1000.mp4`, etc. — easy to compare side-by-side.

**"Better" means:** More Skye-like appearance in the face/gear, clearer and more character-appropriate voice, smoother body motion. **Trust your eyes over the loss number.** The birthday video workflow is the final judge.

---

## Validation Start Frames

The `images:` field in each validation config makes the model generate a video from a specific starting image during training — this is your most direct test of the birthday video workflow.

**Current start frames available:**
- ✅ `skye_helicopter_birthday_gili/start_frame.png` — Skye in cockpit. Already on server at `/home/efrattaig/data/start_frames/skye_helicopter_birthday_gili.png`

**Start frames to create (suggested):**
1. Skye flying outdoors with her helicopter backpack — tests aerial scene
2. Skye portrait / close-up looking directly at camera, smiling — the simplest talking-head test
3. Skye at the Lookout tower with other pups visible — tests group scene
4. Skye in different cockpit angle (side view) — tests if the model generalizes pose

The more varied the test frames, the more you learn about what the LoRA can and can't handle. A LoRA that works only on the training cockpit scene hasn't truly learned Skye.

**How to add a new start frame:**
1. Place the image in `inputs/` locally (e.g. `inputs/skye_portrait/start_frame.png`)
2. `scp` it to the server: `scp inputs/skye_portrait/start_frame.png efrattaig@35.238.2.51:/home/efrattaig/data/start_frames/`
3. Add it to the `images:` list in the validation section of the relevant config YAML
4. The next training run will use it for validation videos

**To enable start-frame validation in EXP-1 (before other frames exist):**
Add to `skye_exp1_baseline.yaml`:
```yaml
validation:
  images:
    - "/home/efrattaig/data/start_frames/skye_helicopter_birthday_gili.png"
    - "/home/efrattaig/data/start_frames/skye_helicopter_birthday_gili.png"
    - "/home/efrattaig/data/start_frames/skye_helicopter_birthday_gili.png"
```
(One image per prompt — the same image is fine; you'll see how it handles each validation prompt from the same start frame.)

---

## Timeline

| When | What | Machine |
|------|------|---------|
| Now running | Preprocessing (~3–4 hrs, 111 clips → latents) | Machine 1 |
| After preprocessing | Verify: `latents/`, `conditions/`, `audio_latents/` all have 111 files | |
| After verify | Launch EXP-1 + EXP-4 in parallel | Machine 1 + 2 |
| +2h (EXP-1 step 500) | Check W&B — is Skye recognizable? → launch EXP-2 | Machine 1 |
| +3h (EXP-1 done) | Archive EXP-1; focus on EXP-2 | |
| +5h (EXP-2 step 1000) | Run `run_skye_like_api.py --lora checkpoint-1000` | Local |
| +7h (EXP-2 step 2500) | Run HQ eval; check W&B vs EXP-4 | |
| +12h (EXP-2 step 5000) | Final HQ eval; decide: done or extend to 10k | |
| If underfitting | Continue EXP-2 to 10k **or** launch EXP-3 | Machine 1 |
| If good | Produce birthday video → `run_skye_like_api.py --lora best_checkpoint` | Local |

---

## Current Status

| Step | Status |
|------|--------|
| ✅ Dry run (10 clips, 100 steps) | Done — pipeline confirmed, W&B working |
| ✅ W&B authenticated | Done — `ltx-2-skye-lora` project live |
| ✅ Full dataset download (111 clips) | Done — `golden_skye_train/` on server |
| ✅ Preview video with subtitles | Done — `output/skye_preview.mp4` |
| ✅ `run_skye_like_api.py --lora` flag | Done — ready for checkpoint evaluation |
| ✅ All 4 experiment configs written | Done — `skye_exp{1-4}_*.yaml` in repo |
| 🔄 **Preprocessing** | Running now — watch: `ssh efrattaig@35.238.2.51 "tail -f /tmp/ltx_preprocess.log"` |
| ⬜ Preprocessing verify (111 files each dir) | Waiting |
| ⬜ EXP-1 + EXP-4 launch | Waiting on preprocessing |
| ⬜ EXP-2 (primary, 5000 steps) | Waiting on EXP-1 step 500 |
| ⬜ HQ evaluation (`run_skye_like_api.py`) | At checkpoints 1000 / 2500 / 5000 |
| ⬜ Birthday video | End goal |

---

## Verify Preprocessing Complete

```bash
ssh efrattaig@35.238.2.51 "
for d in latents conditions audio_latents; do
  echo \"\$d: \$(ls /home/efrattaig/data/golden_skye_preprocessed/\$d 2>/dev/null | wc -l) files\"
done
"
```
Expected: all three dirs show `111 files`. If any count is off, something failed.

---

## Quick Command Reference

```bash
# Watch preprocessing live
ssh efrattaig@35.238.2.51 "tail -f /tmp/ltx_preprocess.log"

# Check if preprocessing is still running
ssh efrattaig@35.238.2.51 "ps aux | grep process_dataset | grep -v grep"

# Launch EXP-1 (after preprocessing done)
ssh efrattaig@35.238.2.51 "cd /home/efrattaig/LTX-2 && \
  nohup /home/efrattaig/.local/bin/uv run python packages/ltx-trainer/scripts/train.py \
  packages/ltx-trainer/configs/skye_exp1_baseline.yaml \
  2>&1 | tee /tmp/ltx_current.log &"

# Watch training live
ssh efrattaig@35.238.2.51 "tail -f /tmp/ltx_current.log"

# HQ evaluation with LoRA checkpoint
uv run python run_skye_like_api.py \
  --lora /home/efrattaig/LTX-2/outputs/skye_exp2_standard/checkpoint-2500/pytorch_lora_weights.safetensors
```

---

## Action Items

> Preprocessing is done — 106/111 clips ready (5 skipped, too short to crop to 2s — normal).

### Ready to launch now

- [ ] **Launch EXP-1 on Machine 1**
  ```bash
  ssh efrattaig@35.238.2.51 "cd /home/efrattaig/LTX-2 && git pull && \
    nohup /home/efrattaig/.local/bin/uv run python packages/ltx-trainer/scripts/train.py \
    packages/ltx-trainer/configs/skye_exp1_baseline.yaml \
    2>&1 | tee /tmp/ltx_current.log &"
  ```

- [ ] **Launch EXP-4 on Machine 2** in parallel with EXP-1 (copy preprocessed data, then same command with `skye_exp4_i2v.yaml`)

### Your task — create start frame images

- [ ] Make **3 more start frame images** for validation:
  1. Skye flying outdoors with helicopter backpack
  2. Skye close-up / portrait looking at camera
  3. Skye at the Lookout tower
  - Save locally to `inputs/`, then `scp` to server `/home/efrattaig/data/start_frames/`
  - Once they exist, add them to the `images:` field in each experiment config

### During EXP-1 (~2 hrs in)

- [ ] **Check W&B at step 500** — is Skye's pink coloring and cockpit visible?
  - Yes → launch EXP-2 immediately (don't wait for EXP-1 to finish)
  - No → investigate captions and audio latents

### After EXP-1 step 500 — launch main experiment

- [ ] **Launch EXP-2 on Machine 1** (5000 steps, primary run)

### At training milestones

- [ ] **HQ eval — EXP-2 checkpoint-1000** → `run_skye_like_api.py --lora checkpoint-1000`
- [ ] **HQ eval — EXP-2 checkpoint-2500** → compare to baseline
- [ ] **HQ eval — EXP-2 checkpoint-5000** → decide: done or extend to 10k steps

### If EXP-2 step 2500 looks underfitting

- [ ] Launch **EXP-3** (attention + FFN layers, contingency only)
