#!/usr/bin/env bash
# stage_for_training.sh — make a dataset trainer-readable.
#
# The LTX-2 trainer (process_dataset.py output / PrecomputedDataset reader)
# hardcodes the subfolder names `latents/` and `conditions/`. Our datasets
# in GCS use the human-readable names `vae_latents_video/` and
# `text_prompt_conditions/`. This script creates the two symlinks that
# bridge the gap on the local training machine.
#
# Run it once after `gcloud storage rsync` brings a dataset to the VM,
# before launching `train.py`. Idempotent — safe to re-run.
#
# Usage:
#   bash stage_for_training.sh /home/efrattaig/training_data_full/chase_golden_v1
set -euo pipefail

DS="${1:-}"
[ -z "$DS" ] && { echo "usage: $0 DATASET_DIR"; exit 2; }
[ -d "$DS/precomputed" ] || { echo "ERROR: $DS/precomputed not found"; exit 3; }

cd "$DS/precomputed"

# vae_latents_video → latents
if [ ! -e latents ]; then
  ln -s vae_latents_video latents
  echo "  symlink: $DS/precomputed/latents → vae_latents_video"
elif [ -L latents ]; then
  echo "  skip: $DS/precomputed/latents already a symlink"
else
  echo "  WARN: $DS/precomputed/latents exists and is not a symlink — leaving alone"
fi

# text_prompt_conditions → conditions
if [ ! -e conditions ]; then
  ln -s text_prompt_conditions conditions
  echo "  symlink: $DS/precomputed/conditions → text_prompt_conditions"
elif [ -L conditions ]; then
  echo "  skip: $DS/precomputed/conditions already a symlink"
else
  echo "  WARN: $DS/precomputed/conditions exists and is not a symlink — leaving alone"
fi

echo "  ready: trainer can now read $DS/precomputed/{latents,conditions}/processed_videos/"
