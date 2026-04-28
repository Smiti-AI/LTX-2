#!/usr/bin/env python3
"""Emit a trainer-input CSV from a dataset's metadata.json, with brand-token
substitution applied at emit time. The CSV is ephemeral — a transient
artifact for `process_dataset.py`, not a persisted dataset asset.

Usage:
    python emit_trainer_csv.py --dataset DATASET_DIR \
        [--out DATASET_DIR/_trainer_input.csv]

Default output is `<dataset>/_trainer_input.csv` (dataset ROOT), because
the trainer resolves the `video_path` column relative to the CSV's own
directory. Putting the CSV at root makes `processed_videos/0001.mp4`
resolve correctly. Idempotent — overwrite OK.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "dataset_pipeline"))
from captions import apply_brand_tokens, load_brand_tokens  # noqa: E402


def emit(dataset_root: Path, out_path: Path) -> int:
    """Emit trainer-input CSV pointing at the LETTERBOXED processed clips
    (processed_videos/), with brand-token substitution applied to captions.

    The trainer's process_dataset.py runs its own scale+crop on whatever
    we hand it; by pre-letterboxing to exact target dimensions, that
    internal step becomes a no-op and the latents reflect the same
    letterboxed pixels you see in training_gallery.mp4."""
    md = json.loads((dataset_root / "metadata.json").read_text())
    token_map = load_brand_tokens()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("w", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerow(["index", "caption", "video_path"])
        for c in md.get("clips", []):
            if c.get("status", "active") != "active":
                continue
            if not (c.get("download_ok", True) and c.get("prompt")):
                continue
            # Verify the processed clip actually exists; skip with warn if not.
            processed = dataset_root / "processed_videos" / f"{c['index']:04d}.mp4"
            if not processed.exists():
                print(f"WARN: skipping clip {c['index']} — {processed} not found "
                      f"(run render_gallery_for_dataset.py first)")
                continue
            caption = apply_brand_tokens(c["prompt"], token_map)
            w.writerow([c["index"], caption, f"processed_videos/{c['index']:04d}.mp4"])
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    root = Path(args.dataset).resolve()
    out = Path(args.out) if args.out else root / "_trainer_input.csv"
    n = emit(root, out)
    token_map = load_brand_tokens()
    print(f"wrote {out} ({n} active clips)")
    print(f"brand-token substitutions applied: {token_map}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
