# Phase 0.1 — Data Inventory Coverage

Total files inventoried: **588 613**
Duplicate hash groups (>1 occurrence): see `duplicates.csv` (~68k from prior run; refresh with `python scripts/phase0/audit_data.py`)

The on-disk inventory is a **merge** of two passes:

- **Local rows** (2 170): fresh scan of current working tree — `inputs/`, `lora_phase_v0/`, `auto_val/`. ~48s wall.
- **GCS rows** (586 443): cached from the initial run at 2026-04-28 13:19 UTC. Wall time ~17 min. Re-run `python scripts/phase0/audit_data.py` to refresh both.

The split was necessary because the local working tree was reorganized (folders moved into `lora_phase_v0/`) between the initial run and the dependent reports. Re-listing GCS would have cost ~17 min for negligible delta.

## Per-source counts

| Kind | Source | Files | Size (GB) | Time (s) |
|---|---|---:|---:|---:|
| local | `/Users/efrattaig/projects/sm/LTX-2/inputs` | 111 | 0.09 | 0.2 |
| local | `/Users/efrattaig/projects/sm/LTX-2/lora_phase_v0` | 1 564 | 8.63 | 45.1 |
| local | `/Users/efrattaig/projects/sm/LTX-2/auto_val` | 495 | 1.20 | 3.1 |
| gcs | `gs://video_gen_dataset/` | 562 733 | 14 314.94 | 1023.8 (cached) |
| gcs | `gs://shapeshifter_model_checkpoints/` | 23 626 | 245 656.20 | 52.9 (cached) |
| gcs | `gs://sm_ml_models/` | 7 | 1.58 | 0.8 (cached) |
| gcs | `gs://encord_dataset/` | 13 | 0.01 | 0.7 (cached) |
| gcs | `gs://videoform/` | 102 | 0.00 | 1.3 (cached) |
| gcs | `gs://shapeshifter-459611-models/` | 4 | 127.67 | 0.8 (cached) |

## Determinism note

A fresh full re-run of `audit_data.py` (no merge) produces a byte-identical CSV given identical filesystem + bucket state. The merged file above is *not* the script's deterministic output — it's a one-time stitch. Re-running the script overwrites it with a clean canonical pass.
