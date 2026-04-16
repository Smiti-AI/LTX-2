# Skye LoRA Training — Experiment Roadmap

**Goal:** Fine-tune LTX-2.3 (22B, audio-video DiT) on Skye-only PAW Patrol footage to generate
character-faithful videos — appearance, distinctive motion, and voice.

**Model:** `/home/efrattaig/models/LTX-2.3/ltx-2.3-22b-dev.safetensors` on server `35.238.2.51`
**Text encoder:** `/home/efrattaig/models/gemma-3-12b-it-qat-q4_0-unquantized`
**Training framework:** `packages/ltx-trainer` (PEFT + Accelerate)
**Prior training:** None — clean slate confirmed (no preprocessed data or LoRA checkpoints found anywhere)

---

## Dataset

| Property | Value |
|---|---|
| Source | `gs://video_gen_dataset/TinyStories/data_sets/golden_skye/videos/skye_golden_dataset_v2.parquet` |
| Total clips | **409 clips** (Skye-only, pre-filtered) |
| Seasons | All 10 PAW Patrol seasons |
| Clip duration | 2.1s – 60.1s, avg **17.6s** |
| Captions | `scene_caption` col — detailed visual descriptions, already high quality |
| Transcripts | `transcript_text` col — Skye's exact dialogue, already present |

> The clips are long (avg 17.6s). Preprocessing crops to the resolution bucket
> frame count (center crop). This is fine — each clip yields a representative
> 2-4s window of Skye in her characteristic poses.

---

## Dry Run — Validate the Pipeline First (10 clips, 100 steps)

Before committing to the full 409-clip preprocessing + 2500-step training run, validate each stage
works end-to-end with a tiny slice of data. This catches GCS auth issues, path errors, and VRAM
problems cheaply.

### Dry run step A — Limit the parquet to 10 clips
```bash
ssh efrattaig@35.238.2.51
cd /home/efrattaig/LTX-2

# Fix GCS auth first (see Step 0)
# Then download just 10 clips:
python3 - << 'EOF'
import pandas as pd, subprocess, json, pathlib

PARQUET = "gs://video_gen_dataset/TinyStories/data_sets/golden_skye/videos/skye_golden_dataset_v2.parquet"
OUT = pathlib.Path("/home/efrattaig/data/skye_dryrun")
OUT.mkdir(parents=True, exist_ok=True)

df = pd.read_parquet(PARQUET).head(10)
records = []
for _, row in df.iterrows():
    gs_uri = str(row["output_video_path"])
    fname  = gs_uri.split("/")[-1]
    local  = OUT / fname
    transcript = str(row.get("transcript_text","") or "").strip()
    scene_cap  = str(row.get("scene_caption","")  or "").strip()
    caption = f'SKYE says "{transcript}". {scene_cap}' if transcript else f"SKYE. {scene_cap}"
    subprocess.run(["gsutil", "cp", gs_uri, str(local)], check=True)
    records.append({"media_path": str(local), "caption": caption})

json.dump(records, open(OUT / "dataset.json", "w"), indent=2)
print(f"Downloaded {len(records)} clips to {OUT}")
EOF
```

### Dry run step B — Preprocess the 10 clips
```bash
uv run python packages/ltx-trainer/scripts/process_dataset.py \
  /home/efrattaig/data/skye_dryrun/dataset.json \
  --resolution-buckets "960x544x49" \
  --model-path /home/efrattaig/models/LTX-2.3/ltx-2.3-22b-dev.safetensors \
  --text-encoder-path /home/efrattaig/models/gemma-3-12b-it-qat-q4_0-unquantized \
  --with-audio \
  --output-dir /home/efrattaig/data/skye_dryrun_preprocessed \
  --load-text-encoder-in-8bit \
  2>&1 | tee /tmp/ltx_current.log
```
Expect: `latents/`, `conditions/`, `audio_latents/` each with ~10 files. Takes 5-15 minutes.

### Dry run step C — Run 100 training steps
Point EXP-1 at the dry-run data by setting `preprocessed_data_root` to
`/home/efrattaig/data/skye_dryrun_preprocessed` and `steps: 100`. Or run inline:
```bash
uv run python packages/ltx-trainer/scripts/train.py \
  packages/ltx-trainer/configs/skye_exp1_baseline.yaml \
  2>&1 | tee /tmp/ltx_current.log
# (temporarily edit the YAML: preprocessed_data_root → skye_dryrun_preprocessed, steps: 100)
```

**Pass criteria:**
- Training starts without OOM or import errors
- Loss decreases over 100 steps
- A validation video is written to `outputs/skye_exp1_baseline/`
- The video is not a blank/black frame

If all pass → proceed to the full run (Steps 1–3 below). Revert the YAML edits first.

---

## Step 0 — Prerequisites (do once on server)

### GCS auth (blocker — data download will fail without this)
```bash
ssh efrattaig@35.238.2.51
gcloud auth application-default login
# Follow the browser OAuth flow, or activate a service account key:
# gcloud auth activate-service-account --key-file=/path/to/key.json
```
Verify access:
```bash
gsutil ls gs://video_gen_dataset/TinyStories/data_sets/golden_skye/ | head -5
```

### W&B (optional — set before training if you want loss curves)
```bash
export WANDB_API_KEY=<your_key>
# Then set wandb.enabled: true in the YAML before running.
# All configs default to wandb.enabled: false — safe to skip.
```

---

## Step 1 — Data Download

Run from `/home/efrattaig/LTX-2` on the server.

### 1a — Download MP4s and build dataset.json
```bash
nohup uv run python packages/ltx-trainer/scripts/prep_data_2_prep_prompts.py \
  --parquet gs://video_gen_dataset/TinyStories/data_sets/golden_skye/videos/skye_golden_dataset_v2.parquet \
  --output-dir /home/efrattaig/data/golden_skye_train \
  2>&1 | tee /tmp/ltx_current.log &
```
Expect ~409 MP4s downloaded. Each clip has its GCS path and `scene_caption` written to `dataset.json`.

### 1b — Enrich captions with Skye's dialogue
The parquet has `transcript_text` (exact dialogue) alongside `scene_caption`. Combine them so
every caption reads: *"SKYE says 'dialogue'. visual description."*
This mirrors the tutorial's approach of embedding the character's speech in captions.

```bash
python3 - << 'EOF'
import json, pandas as pd

PARQUET = "gs://video_gen_dataset/TinyStories/data_sets/golden_skye/videos/skye_golden_dataset_v2.parquet"
DATASET = "/home/efrattaig/data/golden_skye_train/dataset.json"

df = pd.read_parquet(PARQUET)
dataset = json.load(open(DATASET))

# Build lookup: video filename → enriched caption
caption_map = {}
for _, row in df.iterrows():
    fname = str(row["output_video_path"]).split("/")[-1]
    transcript = str(row.get("transcript_text", "") or "").strip()
    scene_cap  = str(row.get("scene_caption",  "") or "").strip()
    if transcript:
        caption_map[fname] = f'SKYE says "{transcript}". {scene_cap}'
    else:
        caption_map[fname] = f"SKYE. {scene_cap}"

for item in dataset:
    fname = item["media_path"].split("/")[-1]
    if fname in caption_map:
        item["caption"] = caption_map[fname]

json.dump(dataset, open(DATASET, "w"), indent=2)
print(f"Done — {len(dataset)} clips updated")
print("Sample:", dataset[0]["caption"][:200])
EOF
```

### 1c — Verify
```bash
python3 -c "
import json
d = json.load(open('/home/efrattaig/data/golden_skye_train/dataset.json'))
print(len(d), 'clips')
print('Caption sample:', d[0]['caption'][:200])
"
# Check video specs of the first clip
FIRST=$(python3 -c "import json; print(json.load(open('/home/efrattaig/data/golden_skye_train/dataset.json'))[0]['media_path'])")
ffprobe -v quiet -print_format json -show_streams "$FIRST" | \
  python3 -c "import sys,json; s=[x for x in json.load(sys.stdin)['streams'] if x['codec_type']=='video'][0]; print(s['width'],'x',s['height'],'@',eval(s['r_frame_rate']),'fps,',s.get('duration','?'),'s')"
```

---

## Step 2 — Preprocessing

Encode video latents, text embeddings, and audio latents for all 409 clips.
This runs the VAE (video + audio) and Gemma feature extractor — expect several hours.

```bash
nohup uv run python packages/ltx-trainer/scripts/process_dataset.py \
  /home/efrattaig/data/golden_skye_train/dataset.json \
  --resolution-buckets "960x544x49;960x544x97" \
  --model-path /home/efrattaig/LTX-2/models/ltx-2.3-22b-dev.safetensors \
  --text-encoder-path /home/efrattaig/LTX-2/models/gemma-3-12b-it-qat-q4_0-unquantized \
  --with-audio \
  --output-dir /home/efrattaig/data/golden_skye_preprocessed \
  --load-text-encoder-in-8bit \
  2>&1 | tee /tmp/ltx_current.log &
```

**Resolution buckets explained:**
- `960x544` — 16:9 at ~540p (PAW Patrol native aspect ratio, divisible by 32)
- `x49` — 2s crop (49 frames at 25fps; valid since 49 % 8 == 1)
- `x97` — 4s crop (97 frames; valid since 97 % 8 == 1)

Clips longer than the bucket are center-cropped. Clips shorter than the smallest bucket are skipped.

**Verify preprocessing output:**
```bash
for d in latents conditions audio_latents; do
  echo "$d: $(ls /home/efrattaig/data/golden_skye_preprocessed/$d 2>/dev/null | wc -l) files"
done
# All three counts should match (one .safetensors per clip per bucket)
```

---

## Step 3 — Training Experiments

All configs are in `packages/ltx-trainer/configs/skye_exp{1-4}_*.yaml`.

**Run template (replace `expN` and config name):**
```bash
cd /home/efrattaig/LTX-2
nohup uv run python packages/ltx-trainer/scripts/train.py \
  packages/ltx-trainer/configs/skye_expN_name.yaml \
  2>&1 | tee /tmp/ltx_current.log &
```

### Execution Order & Decision Tree

```
Data pipeline (Steps 1–2, one-time, ~3-6h total)
        │
        ▼
EXP-1: Baseline  ─── 1000 steps, rank 16 ─── ~1h
        │
        ▼  step 500: is Skye's pink coloring visible?
        │  NO → check captions (Step 1b) and audio_latents before continuing
        │  YES ▼
EXP-2: Standard  ─── 2500 steps, rank 32 ─── ~3-4h   ⭐ main experiment
        │
        ├─ step 1000: evaluate → if still generic, run EXP-3
        ├─ step 2000: evaluate → if good, can stop here
        │
        ▼ underfitting?                    ▼ I2V needed?
EXP-3: High Capacity               EXP-4: I2V Focus
3000 steps, rank 32 + FFN          2500 steps, rank 32
~4-5h                              first_frame_p=0.8
```

---

### EXP-1 — Baseline (fast validation)

**Config:** `packages/ltx-trainer/configs/skye_exp1_baseline.yaml`

| Parameter | Value |
|---|---|
| LoRA rank / alpha | 16 / 16 |
| Target modules | `to_q`, `to_k`, `to_v`, `to_out.0` (all attention) |
| Steps | 1000 |
| Learning rate | 1e-4 |
| Validation every | 100 steps |
| Output | `outputs/skye_exp1_baseline/` |

```bash
nohup uv run python packages/ltx-trainer/scripts/train.py \
  packages/ltx-trainer/configs/skye_exp1_baseline.yaml \
  2>&1 | tee /tmp/ltx_current.log &
```

**Go/no-go at step 500:** Validation video must show a clearly pink character in a cockpit or Skye-like pose.

---

### EXP-2 — Standard ⭐ Primary

**Config:** `packages/ltx-trainer/configs/skye_exp2_standard.yaml`

| Parameter | Value |
|---|---|
| LoRA rank / alpha | 32 / 32 |
| Target modules | `to_q`, `to_k`, `to_v`, `to_out.0` (all attention) |
| Steps | 2500 |
| Learning rate | 1e-4 |
| Validation every | 250 steps |
| Output | `outputs/skye_exp2_standard/` |

```bash
nohup uv run python packages/ltx-trainer/scripts/train.py \
  packages/ltx-trainer/configs/skye_exp2_standard.yaml \
  2>&1 | tee /tmp/ltx_current.log &
```

**Evaluate at:** step 1000 (early check), 2000 (should be "pretty good"), 2500 (final).
Compare against `inputs/BM_v1/benchmark_v1/skye_*/` reference clips.

---

### EXP-3 — High Capacity (run if EXP-2 underfits)

**Config:** `packages/ltx-trainer/configs/skye_exp3_highcap.yaml`

Adds `ff.net.0.proj`, `ff.net.2`, `audio_ff.net.0.proj`, `audio_ff.net.2` to target modules.
This gives the LoRA more capacity to learn Skye's distinctive motion style and expression range.

| Parameter | Value |
|---|---|
| LoRA rank / alpha | 32 / 32 |
| Target modules | attention + FFN (both video and audio branches) |
| Steps | 3000 |
| Learning rate | 1e-4 |
| Output | `outputs/skye_exp3_highcap/` |

---

### EXP-4 — Image-to-Video Focus

**Config:** `packages/ltx-trainer/configs/skye_exp4_i2v.yaml`

Trains with 80% image conditioning (`first_frame_conditioning_p: 0.8`). Use when the production
workflow is: provide a clean Skye start frame → generate animation.

For validation with actual start frames, uncomment the `images:` section in the YAML and point
to frames from `inputs/BM_v1/benchmark_v1/skye_*/start_frame.png`.

| Parameter | Value |
|---|---|
| LoRA rank / alpha | 32 / 32 |
| Target modules | `to_q`, `to_k`, `to_v`, `to_out.0` |
| Steps | 2500 |
| first_frame_conditioning_p | **0.8** |
| Output | `outputs/skye_exp4_i2v/` |

---

## Validation Prompts (all experiments)

All four configs use the same three prompts:

1. *"SKYE, the Cockapoo pup from PAW Patrol, sits in her pink helicopter cockpit with her aviator helmet on. She says 'PAW Patrol, on a roll!' Her big purple eyes light up with excitement as she pulls back on the controls. The audio captures her enthusiastic voice and the hum of the helicopter blades."*

2. *"SKYE from PAW Patrol does an aerial barrel roll with her helicopter backpack, pink fur gleaming in the sunlight, ears streaming behind her as she spins. She calls out 'This pup's gotta fly!' The audio has her joyful laugh and the whirring of her rotors."*

3. *"SKYE sits at the Lookout tower beside Chase, her tail wagging eagerly. She tilts her head and says 'Don't worry, I've got this!' Her purple eyes are wide and bright, full of confidence. The audio captures her clear, cheerful voice and ambient outdoor sounds."*

---

## Phase 2 — Caption Alignment with Transcript Boundaries (deferred)

After EXP-2 validates basic character fidelity, a further quality improvement is available:

The current setup center-crops 17.6s average clips to 2-4s windows. The dialogue in the window
may not match the `transcript_text` caption. Phase 2 fixes this:

1. Split each clip into short segments aligned to dialogue boundaries using `split_scenes.py`
2. Each segment gets the matching transcript line as its caption
3. Re-run `process_captions.py` on the split clips (video latents can be reused)
4. Retrain EXP-2 with the aligned data and compare

This is equivalent to what the tutorial author did manually (writing "ostress says '...'" per clip).
The data to do this already exists in the parquet — no Whisper transcription needed.

---

## Evaluation Checklist

| Checkpoint | What to check |
|---|---|
| After Step 1b | Caption sample starts with `SKYE says "..."` |
| After Step 2 | `latents/`, `conditions/`, `audio_latents/` all have same file count |
| EXP-1 step 500 | Validation video: pink character, cockpit visible |
| EXP-2 step 1000 | Skye recognizable; audio has vocal character |
| EXP-2 step 2500 | Compare with `inputs/BM_v1/benchmark_v1/skye_*/` benchmarks |
| EXP-2 final | Can prompt-direct unseen actions ("Skye waves her paw") |

---

## Key File Reference

| File | Purpose |
|---|---|
| `packages/ltx-trainer/scripts/prep_data_2_prep_prompts.py` | Downloads GCS videos + builds `dataset.json` |
| `packages/ltx-trainer/scripts/process_dataset.py` | Encodes VAE latents + Gemma text embeddings |
| `packages/ltx-trainer/scripts/train.py` | Training entry point |
| `packages/ltx-trainer/configs/skye_exp1_baseline.yaml` | EXP-1 config |
| `packages/ltx-trainer/configs/skye_exp2_standard.yaml` | EXP-2 config (primary) |
| `packages/ltx-trainer/configs/skye_exp3_highcap.yaml` | EXP-3 config (if underfitting) |
| `packages/ltx-trainer/configs/skye_exp4_i2v.yaml` | EXP-4 config (I2V workflow) |
| `LTX_2.3_LoRA_Training_Analysis.md` | Architecture deep-dive (which layers, why) |
| `~/Desktop/.../golden_skye_2.csv` | Local export of the GCS parquet (409 rows) |