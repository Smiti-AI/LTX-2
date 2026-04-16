# Skye LoRA — Full Training Plan

**The Goal:** Generate Skye (PAW Patrol) saying a custom birthday message in a character-faithful video — correct appearance, voice, and motion — from a reference start frame.

**W&B:** [ltx-2-skye-lora](https://wandb.ai/shayzweig-smiti-ai/ltx-2-skye-lora) · **Server:** `35.238.2.51`

---

## Which Scripts Are We Using?

**We use the official Lightricks training scripts from `packages/ltx-trainer/`.** Nothing custom was written for training logic. Here's exactly what runs what:

| Script | What it does | Source |
|--------|-------------|--------|
| `packages/ltx-trainer/scripts/train.py` | **Main training loop** | Official Lightricks |
| `packages/ltx-trainer/scripts/process_dataset.py` | Encode all videos + audio + text into latents | Official Lightricks |
| `packages/ltx-trainer/configs/skye_exp*.yaml` | Training parameters for each experiment | **Written by us**, based on official `ltx2_av_lora.yaml` |
| `run_skye_like_api.py` | Full HQ inference for evaluation | Exists in repo, we added `--lora` flag |

The community tutorial (Ostress) used AI Toolkit, which is a different third-party framework. We use the Lightricks-native trainer, which is purpose-built for LTX-2.3 and handles audio-video joint training correctly.

The configs we wrote live at [packages/ltx-trainer/configs/](packages/ltx-trainer/configs/). You can read them — they are self-documenting YAML files.

---

## The Data

- **111 clips** downloaded from `gs://video_gen_dataset/TinyStories/data_sets/golden_skye/`
- Skye-only, all seasons, avg ~17.6s per clip (center-cropped to 2-4s during preprocessing)
- Each clip has: Skye's exact dialogue + detailed scene description as the caption
- Preview video with subtitles: `output/skye_preview.mp4` — watch this to review data quality

---

## Goals — Short Term and Long Term

### Short-Term Goal (next 2–3 weeks)
**Produce one good birthday message video.**

Specifically: given the start frame `skye_helicopter_birthday_gili.png`, generate a 6–10 second video where Skye is visually faithful to the character, says a custom birthday message in her voice, and looks naturally animated.

Success criteria:
- Pink Cockapoo, aviator helmet, helicopter cockpit — visually unmistakably Skye
- Voice sounds like a young girl's, Skye's energy and pitch
- Mouth moves with the speech
- Smooth motion, no flicker or jitter

### Long-Term Goal
**A reliable production pipeline.** Given any birthday child's name + a short message, generate a 10–15 second Skye video on demand. This means:
- A trained LoRA checkpoint that generalizes (not memorized scenes)
- A reliable inference script (`run_skye_like_api.py`)
- The ability to provide different start frames for different "scenes"

---

## Validation Start Frames

Yes — you should create multiple start frames to test different scenarios. The more varied the test frames, the more you learn about what the LoRA can and can't do.

**Suggested start frames to prepare:**
1. ✅ `skye_helicopter_birthday_gili.png` — already on server. Skye in cockpit, helicopter.
2. Skye flying with her helicopter backpack (outdoors, aerial shot)
3. Skye looking directly at camera, smiling (portrait/talking head style)
4. Skye at the Lookout tower with other pups visible

For each one, we can also write a matching validation prompt (what you want her to do/say in the generated video). More start frames = better evaluation of whether the LoRA generalizes to novel compositions.

**How to add a new start frame:**
1. Place the image in `inputs/` locally
2. `scp` it to `/home/efrattaig/data/start_frames/` on the server
3. Add it to the `images:` list in the config you want to validate with

---

## How to Tell Which Training Was Better

Use **three signals together** — no single one is enough:

### 1. Loss Curve (W&B, during training)
Watch the `train/loss` chart. Expected shape:
- **Drops fast early (steps 1–500):** Model learning basic colors, shape, helicopter
- **Slower descent (500–3000):** Voice character, motion style, identity details
- **Plateau (3000+):** Fine-grained details settling
- **Good plateau range: 0.3–0.5.** Below 0.2 = overfitting risk. Stuck above 0.6 = underfitting.

Compare loss curves across experiments in W&B (they're all in the same project).

### 2. W&B Validation Videos (every 250 steps, automatic)
The training generates videos from the validation prompts at each checkpoint. These run with the simple single-stage pipeline (fast, lower quality than production) — use them to watch the **trend**, not judge final quality.

Ask at each checkpoint:
- Step 500: Can I tell this is a pink dog? Is the helicopter there?
- Step 1000: Does she look like Skye specifically vs. a generic cartoon dog?
- Step 2500: Does the voice sound right? Does she move naturally?
- Step 5000: Does it look like Skye from the show?

### 3. Production Evaluation with `run_skye_like_api.py` (at milestones)
This runs the full 2-stage HQ pipeline — the same quality as production output. Run it at key checkpoints and compare files side-by-side:

```bash
# Baseline (no LoRA) — run once and keep as reference
uv run python run_skye_like_api.py

# After EXP-1 step 1000:
uv run python run_skye_like_api.py \
  --lora /home/efrattaig/LTX-2/outputs/skye_exp1_baseline/checkpoint-1000/pytorch_lora_weights.safetensors

# After EXP-2 step 2500:
uv run python run_skye_like_api.py \
  --lora /home/efrattaig/LTX-2/outputs/skye_exp2_standard/checkpoint-2500/pytorch_lora_weights.safetensors

# After EXP-2 step 5000:
uv run python run_skye_like_api.py \
  --lora /home/efrattaig/LTX-2/outputs/skye_exp2_standard/checkpoint-5000/pytorch_lora_weights.safetensors
```

Output files are named `..._base.mp4`, `..._lora-checkpoint-1000.mp4` etc — easy to compare.

**What "better" looks like:** More Skye-like appearance, clearer voice, smoother motion in the HQ output. Trust your eyes over the loss number.

---

## Experiment Schedule

### Step 0 — Preprocessing (tonight, ~3–4 hrs, runs once)

```bash
ssh efrattaig@35.238.2.51
nohup /tmp/launch_preprocess.sh </dev/null >/dev/null 2>&1 &
# Watch: tail -f /tmp/ltx_preprocess.log
```

This encodes all 111 clips into latents for training. Output: `/home/efrattaig/data/golden_skye_preprocessed/` with `latents/`, `conditions/`, `audio_latents/` — all must have 111 files.

---

### EXP-1 — Baseline (rank 16, 2000 steps)

**Why:** Quick sanity check that the character is being learned on the full dataset. Rank 16 is smaller/faster. If EXP-1 at step 500 shows Skye's pink coloring and cockpit, the data pipeline is confirmed.

**Config:** [packages/ltx-trainer/configs/skye_exp1_baseline.yaml](packages/ltx-trainer/configs/skye_exp1_baseline.yaml)

Key parameters:
```yaml
lora:
  rank: 16
  alpha: 16
  target_modules: [to_k, to_q, to_v, to_out.0]   # attention layers only
optimization:
  steps: 2000
  learning_rate: 1e-4
data:
  preprocessed_data_root: /home/efrattaig/data/golden_skye_preprocessed
```

**Run:**
```bash
# Update launch_train.sh to point to exp1 config, then:
ssh efrattaig@35.238.2.51 "sed -i 's/skye_dryrun/skye_exp1_baseline/' /tmp/launch_train.sh"
nohup /tmp/launch_train.sh </dev/null >/dev/null 2>&1 &
```

**Decision at step 500:** If validation shows Skye's colors and helicopter → launch EXP-2 immediately (don't wait for EXP-1 to finish).

---

### EXP-4 — Image-to-Video Focus (rank 32, 2500 steps) — run in parallel with EXP-1

**Why:** EXP-4 is the most production-relevant experiment. It trains with `first_frame_conditioning_p=0.8` — 80% of steps practice generating from a start frame. This is exactly the birthday message workflow: you provide Skye's portrait, it animates her.

**Config:** [packages/ltx-trainer/configs/skye_exp4_i2v.yaml](packages/ltx-trainer/configs/skye_exp4_i2v.yaml)

Key difference from other experiments:
```yaml
training_strategy:
  first_frame_conditioning_p: 0.8   # 80% of steps use a conditioning start frame
```

**Run on Machine 2 (second server, same preprocessed data copied over).**

**Decision at step 1000:** Does validation video continue motion naturally from the start frame? If yes, extend to 5000 steps.

---

### EXP-2 — Main Character LoRA (rank 32, 5000 steps) ⭐ Primary

**Why 5000 steps:** With 111 clips at batch_size=1, 5000 steps = ~45 exposures per clip. The tutorial needed ~263 reps with 19 clips for "excellent" results; our dataset has more variety (10 seasons, different scenes), so less memorization is needed, but 45 reps is a reasonable target for strong character identity.

**Config:** [packages/ltx-trainer/configs/skye_exp2_standard.yaml](packages/ltx-trainer/configs/skye_exp2_standard.yaml)

```yaml
lora:
  rank: 32
  alpha: 32
optimization:
  steps: 5000   # update from 2500
  learning_rate: 1e-4
```

**Checkpoints saved every 250 steps** — evaluate with `run_skye_like_api.py` at steps 1000, 2500, 5000.

**Decision at step 2500:** If loss is still falling and validation shows clear improvement → extend to 10,000 steps on the same run (just update `steps` in config and continue from checkpoint).

---

### EXP-3 — High Capacity (rank 32 + FFN, 3000 steps) — contingency only

**Run only if EXP-2 looks generic at step 2500.** Adds feed-forward layers (the "motion memory") to the LoRA targets.

**Config:** [packages/ltx-trainer/configs/skye_exp3_highcap.yaml](packages/ltx-trainer/configs/skye_exp3_highcap.yaml)

---

### Parallel Execution (two machines)

```
Machine 1:  [Preprocessing] → [EXP-1, 2000 steps] → [EXP-2, 5000 steps]
Machine 2:  [Copy preprocessed data] → [EXP-4, 2500–5000 steps]
```

Machine 2 needs:
```bash
# On Machine 1 — copy preprocessed data to Machine 2
rsync -avz /home/efrattaig/data/golden_skye_preprocessed/ user@machine2:/data/golden_skye_preprocessed/
# Then git pull on Machine 2 to get the configs, and launch EXP-4
```

---

## Timeline

| When | What | Where |
|------|------|-------|
| Tonight | Preprocessing (3–4 hrs) | Machine 1 |
| Tomorrow morning | Review preprocessing output, launch EXP-1 + EXP-4 | Both machines |
| +5 hrs (EXP-1 step 500) | Check W&B — is Skye recognizable? | |
| +10 hrs (EXP-1 done) | Launch EXP-2 on Machine 1 | |
| +12 hrs (EXP-2 step 2500) | Run `run_skye_like_api.py --lora checkpoint-2500` | |
| +24 hrs (EXP-2 step 5000) | Run HQ eval, compare to baseline and EXP-4 | |
| If good | Done — produce birthday video | |
| If underfitting | Continue EXP-2 to 10k, or launch EXP-3 | |

---

## Current Status

| Step | Status |
|------|--------|
| ✅ Dry run (10 clips, 100 steps) | Done — pipeline confirmed |
| ✅ FFmpeg installed | Done |
| ✅ W&B authenticated | Done |
| ✅ Full download (111 clips) | Done |
| ✅ Preview video with subtitles | Done — `output/skye_preview.mp4` |
| ✅ `run_skye_like_api.py --lora` flag | Done |
| 🔄 **Start preprocessing** | Ready to launch |
| ⬜ EXP-1 + EXP-4 (parallel) | Waiting on preprocessing |
| ⬜ EXP-2 (main) | Waiting on EXP-1 step 500 |
| ⬜ Production birthday video | End goal |
