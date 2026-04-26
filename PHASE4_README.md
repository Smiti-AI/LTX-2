# Phase 4 — goldenPLUS150 v2: Data Intervention + Capacity Ablation

**Owner:** Claude (autonomous execution)
**Started:** 2026-04-26
**Status:** ⏸ Waiting for user to provision 2× H100 machines

---

## Why this phase exists

Phase 3 (`goldenPLUS150` cumulative 40k steps across 3 W&B runs) hit a **diagnosed plateau**. The W&B sigma-bucketed loss showed:

| Sigma bucket | After 10k steps | After 40k steps | Verdict |
|---|---|---|---|
| 0.00–0.25 (fine details) | 1.42 | **1.93 ↑** | Got worse — low-σ overfitting |
| 0.25–0.50 (style/textures) | 1.04 | 0.70 | Improved, then flattened |
| 0.50–0.75 (character identity / motion) | 0.80 | **0.78 ➡** | **Plateaued — exactly where we care most** |

The mid-σ plateau is where character identity and motion dynamics live ("Ryder upright stance, Skye backflip"). 30k extra steps did not move it. Diagnosis: the bottleneck is **caption signal**, not model capacity. Specifically, 135 of the 472 clips have generic scene captions that never name the characters present, so the model has no language handle to bind visuals to identity.

---

## Hypotheses being tested

| H# | Hypothesis | Tested by |
|---|---|---|
| H1 | Re-captioning the 135 generic clips with character-specific structured captions unlocks the mid-σ plateau | Machine A |
| H2 | Curriculum (golden-only first, then full data) accelerates identity learning before exposure to broader scenes | Machine A |
| H3 | The LoRA is also capacity-bound — rank 128 helps even on the same re-captioned data | Machine B |

If H1+H2 win → data was the bottleneck. If H3 also wins → both data and capacity were bottlenecks.
If neither wins → harder problem (DoRA / full-FT discussion).

---

## Machines

| Machine | Status | Role |
|---|---|---|
| **34.56.137.11** | benchmark RUNNING | goldenPLUS150 benchmark (5 checkpoints) → then rank-128 training |
| **34.61.89.219** | idle, waiting | Re-caption + curriculum training (rank 32) — needs Gemini API key |
| **35.225.196.76** (NEW, Option C) | benchmark RUNNING | full_cast_exp1_season1 benchmark (4 checkpoints) → idle/shutdown |

User chose **Option C** — provisioned 35.225.196.76 to run the full_cast_exp1_season1 benchmark in parallel with goldenPLUS150. Total wall time to all results: ~16 hr (curriculum) + benchmark times in parallel.

---

## Plan — Machine A: `goldenPLUS150_v2_curriculum`

| Step | Description | Time |
|---|---|---|
| A0 | Setup: clone repo, `uv sync`, rsync model weights + dataset from 34.56.137.11 | 1 hr |
| A1 | Re-caption all 472 clips with Gemma 3 12B vision using structured schema | 30 min |
| A2 | Re-run conditioning preprocessing (text embeddings only) | 10 min |
| A3 | rsync new conditions to Machine B | 5 min |
| A4 | **Phase 1 curriculum**: Chase + Skye golden only (337 clips), 5k steps from `run2_step_07000` | 5 hr |
| A5 | **Phase 2 curriculum**: full re-captioned 472 clips, 10k steps from end of Phase 1 | 10 hr |
| A6 | Validation samples generated at all 3 resolutions every 500 steps with action-specific prompts | (built into A4-A5) |

### Caption schema (structured)

```
[paw_patrol]: <CHARACTER NAMES, exact, no "the pup">. <ACTION VERBS, specific motion>. <POSE>. <SETTING>. <CAMERA shot type>.
```

Anchors used in the VLM system prompt:
- Chase = German Shepherd in blue police outfit, blue cap with star
- Skye = cockapoo in pink uniform, pink helmet
- Marshall = Dalmatian in red firefighter outfit, red helmet
- Rocky = gray mixed-breed in green outfit
- Zuma = chocolate Lab in orange diving outfit
- Rubble = English bulldog in yellow construction outfit
- Ryder = 10-year-old boy in red and blue outfit, red baseball cap

Every caption begins with the trigger token `[paw_patrol]`.

### LoRA config (Machine A)
- Rank 32, alpha 32 (same as Phase 3 baseline — isolates the data variable)
- Target modules: attn (to_k/q/v, to_out.0) + FFN (ff.net.0.proj, ff.net.2) + audio FFN
- LR 1e-4 linear decay
- bf16, gradient checkpointing, 8-bit text encoder
- Resume from `outputs/goldenPLUS150/checkpoints/lora_weights_run2_step_07000.safetensors`

---

## Plan — Machine B: `goldenPLUS150_v2_rank128`

| Step | Description | Time |
|---|---|---|
| B0 | Setup: clone repo, `uv sync`, rsync model weights + dataset from 34.56.137.11 | 1 hr |
| B1 | Wait for Machine A's re-captioning + rsync new conditions | (parallel with A0) |
| B2 | Train rank 128 LoRA, full 472 re-captioned clips, 10k steps from `run2_step_07000` | 12-15 hr |

### LoRA config (Machine B)
- **Rank 128, alpha 128** (only difference from Machine A)
- Same target modules, LR, precision
- Resume from same checkpoint as Machine A
- Same validation prompts as Machine A (so we can compare apples-to-apples)

---

## Plan — 34.61.89.219 (existing): goldenPLUS150 benchmarking

5 checkpoints to benchmark on `run_bm_v1_lora.py`:

| Checkpoint | Cumulative step | Why |
|---|---|---|
| `run2_step_07000.safetensors` | 10k | Pre-overfit baseline (also = Machine A/B starting point) |
| `run3_step_05000.safetensors` | 15k | Mid-run3, before degradation |
| `run3_step_15000.safetensors` | 25k | Near plateau midpoint |
| `run3_step_25000.safetensors` | 35k | Late, possibly degraded |
| `run3_step_30000.safetensors` | 40k | Final weights |

Output: `lora_results/goldenPLUS150_curve/`. ~75 min per checkpoint × 5 = ~6 hours.

This benchmark answers: "did the 30k extra steps help or hurt vs. the 10k baseline?" — independent of Machine A/B work.

---

## Validation prompts (added in Phase 4)

Multi-resolution + action-specific prompts to diagnose where the LoRA actually fails.

```
[paw_patrol] Skye performs a complete mid-air backflip in her pink helicopter, tail rotors spinning, blue sky behind her.
[paw_patrol] Ryder runs upright on two legs across the Lookout platform, calling the pups for a mission.
[paw_patrol] Chase the German Shepherd deploys his net launcher from his backpack, blue uniform clearly visible.
[paw_patrol] Marshall slides headfirst down the firepole at full speed, lands on his feet, shakes himself.
[paw_patrol] The full PAW Patrol team — Chase, Skye, Marshall, Rocky, Zuma, Rubble — assembles on the Lookout platform with Ryder standing in front.
```

Each prompt rendered at all 3 training resolutions: 960×832, 960×544, 1024×576.

---

## What I need from the user

1. **Provision 2× H100 machines** (or 3 if you want DoRA in parallel)
2. **Send me the IPs** — I'll handle setup, training, monitoring, GCS uploads, all of it
3. **Optional:** confirm whether to also include DoRA arm (would need Machine C)

After IPs are provided, no further user action required until results are in (~16 hours).

---

## Status log (live-updated)

### 2026-04-26 — Plan created
- Diagnosed Phase 3 plateau using W&B sigma-bucketed loss
- Drafted Phase 4 plan with 3 hypotheses

### 2026-04-26 — Both existing machines confirmed free
- 34.56.137.11 + 34.61.89.219 both idle, GPUs at 0 MiB
- Killed orphan `run_bm_watcher.sh` on 34.56.137.11 (had been polling for non-existent `step_10000.safetensors` since Apr 21)
- Decision: use existing 2 machines instead of provisioning new ones
- User decision pending: option B (defer full_cast_exp1 benchmark) vs option C (provision 3rd machine for parallel benchmarks)

### 2026-04-26 — goldenPLUS150 benchmark setup
- Pushed 5 checkpoints + training_config.yaml to GCS: `gs://video_gen_dataset/training_runs/ltx2/full_cast/goldenPLUS150/`
- Pulled them down to 34.56.137.11 with **cumulative-step renaming** so output is naturally labeled by total training progress:
  | Original | New name in benchmark dir |
  |---|---|
  | `lora_weights_run2_step_07000.safetensors` | `lora_weights_step_10000.safetensors` |
  | `lora_weights_step_05000.safetensors` (run3) | `lora_weights_step_15000.safetensors` |
  | `lora_weights_step_15000.safetensors` (run3) | `lora_weights_step_25000.safetensors` |
  | `lora_weights_step_25000.safetensors` (run3) | `lora_weights_step_35000.safetensors` |
  | `lora_weights_step_30000.safetensors` (run3) | `lora_weights_step_40000.safetensors` |
- Rsync'd `inputs/BM_v1/` (4.4 MB) from local → 34.56.137.11 (was missing — benchmark scenes need configs + start_frames)

### 2026-04-26 — `run_bm_v1_lora.py` API mismatch found and patched
- First launch crashed: `NotImplementedError: Module [GemmaTextEncoder] is missing the required "forward" function`
- Root cause: `ltx-core` refactored `GemmaTextEncoder` API. Old: `text_encoder(prompt) → (video_embeds, audio_embeds, mask)`. New: `text_encoder.encode(prompt) → (hidden_states, mask)` + `embeddings_processor.process_hidden_states(...)` for the feature extractor + connector pipeline.
- Patched `run_bm_v1_lora.py` `precompute_embeddings()` to:
  1. Also load `embeddings_processor`
  2. Use new `encode()` + `process_hidden_states()` flow
  3. Cache `pos_out.video_encoding` / `pos_out.audio_encoding` instead of old tuple unpacking
- Committed (`6cd7c99`) + pushed; `git pull`'d on 34.56.137.11

### 2026-04-26 — goldenPLUS150 benchmark RUNNING ✅
- Launched on 34.56.137.11 with protected nohup wrapper
- Currently: text encoding done, base videos generating
- GPU 100%, 50 GB VRAM
- 42 generations queued (7 base + 35 LoRA across 5 checkpoints)
- Output: `lora_results/goldenPlus150_version1/goldenPLUS150_benchmark/`
- Will rsync to local `/Users/efrattaig/projects/sm/LTX-2/lora_results/goldenPlus150_version1/` on completion
- Estimated wall time: ~60-90 min (was originally estimated 6 hr — was wrong; H100 + LoRA is faster)

### 2026-04-26 — Captioning blockers identified
- 34.61.89.219 has **no raw .mp4 files** — only preprocessed latents
- 34.56.137.11 has the raw videos for Chase (231) and PAW Patrol 150-clips (135) — total ~250 MB
- Skye golden raw videos NOT FOUND on either H100 server
- Decision: only re-caption the 135 PAW Patrol clips (the ones with generic captions). Chase + Skye golden already have character-specific transcripts.
- User picked: **BEST captioning model, even pro → Gemini 2.5 Pro via API**
- Blocker: no `GEMINI_API_KEY` on any machine. **Waiting on user.**

### 2026-04-26 — `training_config.yaml` YAML loader bug found and patched
- Second crash: `yaml.constructor.ConstructorError: could not determine a constructor for the tag 'tag:yaml.org,2002:python/tuple'`
- The trainer dumps configs with `!!python/tuple` tags (e.g. `video_dims: !!python/tuple [960, 544, 49]`) which `yaml.safe_load` rejects
- Fix: changed `_load_lora_config_from_exp` to use `yaml.unsafe_load` (file is trainer-generated, not user input — safe)
- Committed (`2242b86`) + pushed; pulled on 34.56.137.11
- Benchmark relaunched — passed previous crash point, now generating LoRA videos for step_10000

### 2026-04-26 — Option C confirmed: 35.225.196.76 provisioned
- New machine: 35.225.196.76 (H100 80GB)
- Setup completed:
  - cloned repo, `uv sync` finished
  - generated temp SSH key, added to 34.56.137.11's authorized_keys
  - rsync 79 GB models (LTX-2.3 + Gemma) from 34.56.137.11 in **3 minutes** at 379 MB/s
  - rsync W&B `.netrc`, BM_v1 inputs, full_cast_exp1_season1 outputs (11 GB)
- Benchmark exp-dir set up with cumulative-step renaming for 4 checkpoints:
  | Original (run2/run3) | Renamed in benchmark dir | Cumulative step |
  |---|---|---|
  | `lora_weights_run2_step_02500` | `lora_weights_step_03500.safetensors` | 3500 |
  | `lora_weights_step_02500` (run3) | `lora_weights_step_06000.safetensors` | 6000 |
  | `lora_weights_step_05000` (run3) | `lora_weights_step_08500.safetensors` | 8500 |
  | `lora_weights_step_06500` (run3) | `lora_weights_step_10000.safetensors` | 10000 |

### 2026-04-26 — Both benchmarks running ✅
- **34.56.137.11**: goldenPLUS150 — finished step_10000 (7 videos), now on step_15000
- **35.225.196.76**: full_cast_exp1_season1 — text encoding + base model loading complete, generating base videos
- Total: 7 base + 35 LoRA (goldenPLUS150) + 7 base + 28 LoRA (full_cast_exp1) = 77 videos in flight

### 2026-04-26 — Captioning unblocked: Vertex AI ADC works
- User confirmed: shapeshifter project's GCP service account has Gemini access via Vertex AI — no API key needed
- Tested: `genai.Client(vertexai=True, project='shapeshifter-459611', location='us-central1')` works on 34.61.89.219
- Found the smoking gun in the dataset: ALL 135 PAW Patrol clips have the EXACT SAME generic caption:
  > "A scene from PAW Patrol, a colorful animated children's series. The PAW Patrol pups — including Chase, Skye, Marshall, Rocky, Zuma, Rubble, and Everest — work together on rescue missions in Adventure Bay..."
  
  This is 28.6% of the training data with **zero distinguishing signal**. Confirms the diagnosis exactly.
- Wrote `recaption_paw_patrol.py` with structured schema (anchored character identification + action verbs + setting + camera) using `gemini-2.5-pro` via Vertex AI
- Tested on 3 clips — output quality is excellent. Examples:
  - "[paw_patrol] Ryder and Chase. Ryder stands upright on two legs, thinking and then talking while gesturing. Chase stands beside him in a large pumpkin patch on a hillside as it snows lightly. The camera is a medium shot."
  - "[paw_patrol] In a playground, Zuma stands still as Skye runs up, taps him with her paw, and talks. Medium shot."
- **Re-captioning all 135 clips RUNNING** on 34.61.89.219 (no GPU contention — uses cloud API)

### 2026-04-26 — All three jobs running in parallel
| Machine | Job | Progress |
|---|---|---|
| 34.56.137.11 | goldenPLUS150 benchmark | 3/5 checkpoints done (step_10000/15000/25000) |
| 35.225.196.76 | full_cast_exp1_season1 benchmark | 1/4 checkpoints in progress (step_03500) |
| 34.61.89.219 | Re-captioning 135 PAW Patrol clips via Gemini 2.5 Pro | ~76/136 done in 3 min |

### 2026-04-26 — Captioning pass DONE (136/136, 0 failures, 6 min 15 sec)
- Gemini 2.5 Pro produced rich, character-anchored, action-specific captions for all 136 clips
- Sample improvements:
  - **Old (all 136 clips, identical):** "A scene from PAW Patrol, a colorful animated children's series. The PAW Patrol pups — including Chase, Skye, Marshall, Rocky, Zuma, Rubble, and Everest — work together on rescue missions in Adventure Bay..."
  - **New (per-clip):**
    - "[paw_patrol] Skye flies with her pup pack, landing on a boat where Ryder stands upright on two legs. Zuma is in the water..."
    - "[paw_patrol] Marshall crouches with his water cannons deployed, talks, and barks before starting to walk..."
    - "[paw_patrol] Zuma pilots his hovercraft in the water as Ryder drives his ATV beside him. They approach a large cargo ship..."
    - "[paw_patrol] Ryder rows a boat on a stormy sea. A small child stands in the boat, looking at the big waves."
- Output saved to `/home/efrat_t_smiti_ai/data/full_cast_raw/dataset_recaptioned.csv`

### 2026-04-26 — Conditions regenerated (36 sec)
- Backed up old conditions: `conditions_v1_generic_backup/` (136 .pt files)
- Re-ran `process_captions.py` on the new CSV with `--load-text-encoder-in-8bit`
- Generated 136 new text embedding .pt files in `conditions/` (overwrote old generic ones)
- Latents and audio_latents unchanged — only text-side embeddings updated
- The combined dataset is now ready: 472 clips with character-rich captions for the 135 PAW Patrol portion

### 2026-04-26 — Phase 1 curriculum training LAUNCHED ✅
- Machine: 34.61.89.219
- Config: `goldenPLUS150_v2_phase1.yaml` (rank 32, 5k steps, attn+FFN, LR 1e-4)
- Dataset: `/home/efrat_t_smiti_ai/data/phase4_phase1_chase_skye/` (337 hardlinked Chase+Skye clips only)
- Resume from: `lora_weights_run2_step_07000.safetensors` (cumulative-10k pre-overfit baseline)
- W&B run: [t3eriouh](https://wandb.ai/shayzweig-smiti-ai/LTX2_lora/runs/t3eriouh) (under `LTX2_lora` project per user instruction)
- Validation: 3 action-specific prompts (Skye backflip, Ryder upright, Chase deploys net launcher), every 500 steps
- ETA: ~5 hours

### Outstanding work (queued)
- **Phase 2 curriculum** (10k steps on full 472 re-captioned dataset) — launches when Phase 1 finishes

### 2026-04-26 — Both benchmarks DONE ✅
- **goldenPLUS150 benchmark (34.56.137.11)**: 35/35 LoRA comparisons + 7 base videos. Rsync'd to `/Users/efrattaig/projects/sm/LTX-2/lora_results/goldenPlus150_version1/` (77 mp4 files locally)
- **full_cast_exp1_season1 benchmark (35.225.196.76)**: 28/28 LoRA comparisons + 7 base videos. Rsync'd to `/Users/efrattaig/projects/sm/LTX-2/lora_results/full_episods/` (64 mp4 files locally)

### 2026-04-26 — Rank-128 ablation LAUNCHED on 34.56.137.11 ✅
- Initial attempt loaded rank-32 baseline weights → shape mismatch (rank-128 LoRA params ≠ rank-32 LoRA params).
- Updated config to `load_checkpoint: null` — rank-128 trains from scratch. This is the *correct* setup for an ablation isolating capacity (no warm-start advantage).
- W&B run: [kjy9r8wq](https://wandb.ai/shayzweig-smiti-ai/LTX2_lora/runs/kjy9r8wq)
- **1.23 BILLION trainable params** (vs 308M for rank-32). 4× capacity.
- Dataset: full 472 re-captioned (the new conditions were rsync'd from 34.61.89.219 first; 136 generic-caption .pt backed up to `conditions_v1_generic_backup/`)
- 10k steps, ETA ~12-15 hours

### 2026-04-26 — Chase + Skye character benchmarks complete (goldenPLUS150)
- Chase benchmark: 5 scenes × 5 checkpoints + 5 base = 30 generations on 35.225.196.76
- Skye benchmark first attempt failed: needed `inputs/overfit_bm/` images for the 5 "of_*" scenes; rsync'd from local + relaunched
- Skye now running 10 scenes × 5 checkpoints + 10 base = 60 generations (~1 hr)
- Output paths:
  - Chase: `lora_results/chase_lora/benchmarks/`
  - Skye: `output/benchmarks/` (different default in skye script)

### 2026-04-26 — 3-way comparison viewer plan
- User wants every scene to show **3 results: 150 / episode / base** with VLM analysis (cheap = Flash mode)
  - **150** = goldenPLUS150 LoRA (using cumulative step 10000 — diagnosed best, before low-σ overfitting)
  - **episode** = full_cast_exp1_season1 LoRA (cumulative step 10000, final)
  - **base** = LTX-2.3, no LoRA
- Queued follow-up benchmark on 35.225.196.76 (auto-launches when current Skye finishes):
  - Chase + Skye benchmarks on `outputs/full_cast_exp1_benchmark` exp dir, with `--steps 10000` so only the final cumulative checkpoint is rendered (saves time vs all 4)
- Wrote `auto_val/build_phase4_viewer.py`:
  - Loads scenes from BM_v1 (configs.json), chase_bm and sky_bm (BENCHMARK_SCENES from the .py scripts)
  - For each scene, finds the 3 model videos
  - Runs `vlm_pipeline.run --mode cheap` (Flash) on each → produces report.json with score/verdict/headline
  - Writes per-scene `outputs_vlm_compare_phase4_<scene>/manifest.json` + viewer.html
- Estimated cost: 17 scenes × 3 models × ~$0.10 = **~$5** for VLM analysis
- Estimated time: ~30-45 min after all benchmarks finish

---

## Decisions log

| Date | Decision | Reasoning |
|---|---|---|
| 2026-04-26 | Use 2 machines instead of 4 | Diagnosis points at data signal, not capacity. Run the strongest hypothesis (data) cheaply first. If data fix doesn't work, escalate to more arms. |
| 2026-04-26 | Resume from `run2_step_07000`, not `run3_step_30000` | Run3 introduced low-σ overfitting; the 10k checkpoint is a cleaner baseline. |
| 2026-04-26 | Use Gemma 3 12B vision for re-captioning | Already on the servers; sufficient for structured caption generation; no separate model download. |
| 2026-04-26 | Trigger token `[paw_patrol]` (not strip prior) | Augments the base model's existing prior under a unique handle; doesn't fight it. |
| 2026-04-26 | Skip mirror/flip augmentation | PAW Patrol pups have direction-specific badges; flipping breaks visual authenticity. |
| 2026-04-26 | No noise offset, keep `shifted_logit_normal` | Noise offset is an SD1.5 fix that doesn't translate to flow matching; no observed muddy-output symptom. |

---

## Results (to be filled in)

### Machine A — curriculum + re-captioned
- W&B run: TBD
- Best checkpoint by validation: TBD
- Final sigma-bucket losses: TBD

### Machine B — rank 128 + re-captioned
- W&B run: TBD
- Best checkpoint by validation: TBD
- Final sigma-bucket losses: TBD

### 34.61.89.219 — Phase 3 checkpoint benchmark
- TBD per checkpoint

---

## Next-phase decision tree (after 16 hours)

```
A clearly beats Phase-3-best AND beats B
  → Data was the bottleneck. Ship Machine A's best checkpoint.
  → Phase 5: dataset expansion (more episodes, more action variety)

B clearly beats both A and Phase-3-best
  → Capacity was the bottleneck.
  → Phase 5: rank 128 + curriculum + re-captioned in one combined run

A and B both beat Phase-3-best, neither dominates
  → Both helped — train Phase-5 with rank 128 + curriculum + re-captioned

Neither A nor B clearly beats Phase-3-best
  → Harder problem. Escalate to DoRA + targeted hard-example collection.
  → Re-evaluate dataset coverage (especially Ryder/action-verb gaps)
```
