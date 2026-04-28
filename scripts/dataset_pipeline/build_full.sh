#!/usr/bin/env bash
# End-to-end build for one character at full scale on the GPU VM.
# Designed to run via nohup; idempotent.
#
# Usage:
#   bash build_full.sh chase   # → ~/training_data_full/chase_golden_v1
#   bash build_full.sh skye    # → ~/training_data_full/skye_golden_v2
#
# Stages:
#   1. Build raw + metadata via scripts/dataset_pipeline/build_poc.py --limit 0
#   2. Preprocess (raw → processed_videos/) + render both galleries
#   3. Emit _trainer_input.csv (brand-token substitution at this step)
#   4. Encode latents + Gemma conditions via the trainer's process_dataset.py
#
# Logs: /tmp/build_full_<character>.log (tee'd at every step)
set -euo pipefail

CHAR="${1:-}"
case "$CHAR" in
  chase) DS_NAME="chase_golden_v1"; MANIFEST="gs://video_gen_dataset/dataset/labelbox/chase_golden.json" ;;
  skye)  DS_NAME="skye_golden_v2";  MANIFEST="gs://video_gen_dataset/dataset/labelbox/skye_golden.json"  ;;
  *) echo "usage: $0 {chase|skye}" >&2; exit 2 ;;
esac

ROOT="/home/efrattaig/training_data_full"
DS="$ROOT/$DS_NAME"
LOG="/tmp/build_full_${CHAR}.log"
mkdir -p "$ROOT"

echo "=== build_full $CHAR → $DS ===" | tee "$LOG"
date | tee -a "$LOG"

# Stage 1: raw downloads + metadata.json
echo "[1/4] build_poc --character $CHAR --limit 0" | tee -a "$LOG"
python3 ~/LTX-2/scripts/dataset_pipeline/build_poc.py \
  --character "$CHAR" --limit 0 --no-upload \
  --local-out "$ROOT" 2>&1 | tee -a "$LOG"

# Stage 2: preprocess + dual gallery
echo "[2/4] render_gallery_for_dataset (preprocess + source/training galleries)" | tee -a "$LOG"
python3 ~/LTX-2/scripts/dataset_pipeline/render_gallery_for_dataset.py \
  --dataset "$DS" 2>&1 | tee -a "$LOG"

# Stage 3: trainer-input CSV (brand-token substitution applied here)
echo "[3/4] emit_trainer_csv" | tee -a "$LOG"
python3 ~/LTX-2/scripts/dataset_pipeline/emit_trainer_csv.py \
  --dataset "$DS" 2>&1 | tee -a "$LOG"

# Stage 4: latent + condition encoding (GPU)
echo "[4/4] process_dataset (latents + conditions)" | tee -a "$LOG"
cd ~/LTX-2/packages/ltx-trainer
/home/efrattaig/.local/bin/uv run python scripts/process_dataset.py \
  "$DS/_trainer_input.csv" \
  --resolution-buckets 960x544x49 \
  --model-path /home/efrattaig/models/LTX-2.3/ltx-2.3-22b-dev.safetensors \
  --text-encoder-path /home/efrattaig/models/gemma-3-12b-it-qat-q4_0-unquantized \
  --caption-column caption \
  --video-column video_path \
  --output-dir "$DS/precomputed" \
  --batch-size 1 2>&1 | tee -a "$LOG"

echo "=== build_full $CHAR DONE ===" | tee -a "$LOG"
date | tee -a "$LOG"
echo "Final tree:" | tee -a "$LOG"
find "$DS" -maxdepth 4 -type f | head -30 | tee -a "$LOG"
du -sh "$DS"/* 2>&1 | tee -a "$LOG"
