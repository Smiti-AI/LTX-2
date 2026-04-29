#!/usr/bin/env bash
# Pull a built dataset from the VM, push to GCS via local creds. Used
# because the VM's instance SA can't write into
# gs://video_gen_dataset/TinyStories/training_data/, but the developer's
# local user account can.
#
# Usage:
#   bash upload_dataset_to_gcs.sh chase_golden_v1
#   bash upload_dataset_to_gcs.sh skye_golden_v2
set -euo pipefail

DS_NAME="${1:-}"
[ -z "$DS_NAME" ] && { echo "usage: $0 DATASET_NAME"; exit 2; }

VM="efrattaig@35.238.2.51"
VM_PATH="/home/efrattaig/training_data_full/$DS_NAME"
LOCAL_STAGING="/tmp/training_data_full/$DS_NAME"
GCS_DST="gs://video_gen_dataset/TinyStories/training_data/$DS_NAME"

echo "=== upload $DS_NAME ==="
mkdir -p "$LOCAL_STAGING"

# Pull from VM with --partial so dropped connections resume mid-file.
# Wrapped in retry loop because residential ISPs love to RST mid-transfer.
echo "[1/2] rsync pull from VM (--partial + retries)"
mkdir -p "$LOCAL_STAGING/.rsync-partial"
attempt=0
until rsync -avL --partial --partial-dir="$LOCAL_STAGING/.rsync-partial" \
        --timeout=120 \
        -e "ssh -o ServerAliveInterval=30 -o ServerAliveCountMax=10" \
        --exclude '_qa_render_cache' \
        --exclude '_trash' \
        --exclude '_trainer_input.csv' \
        --exclude '.rsync-partial' \
        "$VM:$VM_PATH/" "$LOCAL_STAGING/"; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 6 ]; then
    echo "ERROR: rsync failed 6 times; giving up." >&2
    exit 1
  fi
  echo "  rsync attempt $attempt failed, retrying in 10s…"
  sleep 10
done
rm -rf "$LOCAL_STAGING/.rsync-partial"

du -sh "$LOCAL_STAGING"/* 2>&1

# Push to GCS using local user creds.
echo "[2/2] gcloud storage cp → $GCS_DST"
# Top-level files
for f in metadata.json source_gallery.mp4 training_gallery.mp4; do
  if [ -f "$LOCAL_STAGING/$f" ]; then
    gcloud storage cp "$LOCAL_STAGING/$f" "$GCS_DST/$f" --verbosity=warning 2>&1 | tail -1
  fi
done
# Subdirs
for sub in videos processed_videos precomputed; do
  if [ -d "$LOCAL_STAGING/$sub" ]; then
    gcloud storage rsync "$LOCAL_STAGING/$sub" "$GCS_DST/$sub" --recursive --verbosity=warning 2>&1 | tail -2
  fi
done

echo "=== $DS_NAME uploaded ==="
gcloud storage ls --recursive "$GCS_DST" 2>&1 | grep -v "/$" | head -20
