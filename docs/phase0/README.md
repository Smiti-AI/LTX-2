# Phase 0 — Discovery & Foundation

`active_plan.md §3` — surface ground truth before designing scaffolding. No training, no MLOps decisions yet. The four reports below land first; Phase 1 design comes after the user reviews them.

## Reports

| § | Report | Status | What it answers |
|---|---|---|---|
| 3.1 | [`coverage.md`](coverage.md) (sidecar to `data_inventory.csv`) | done | What exists, where, deduplicated by md5 |
| 3.2 | [`Phase0_QualityReport.md`](Phase0_QualityReport.md) | done (local-only; GCS deferred) | Sharpness / motion / letterbox / audio per local PP video; 50/70/30 bucket shortfall |
| 3.3 | [`Phase0_ResolutionFPS.md`](Phase0_ResolutionFPS.md) | done | Native res/fps distribution; LTX-2 training res cited; 9:16 reframing trade-offs; recommended (W,H,FPS) |
| 3.4 | [`Phase0_BrandTokens.md`](Phase0_BrandTokens.md) | done | Gemma tokenizer behavior on each candidate; recommendation: `CHASE_PP` / `SKYE_PP` (no tokenizer mod) |
| 3.5 | [`Phase0_AnchorGap.md`](Phase0_AnchorGap.md) | done (target split awaits sign-off) | (character × shot type) gap matrix vs the 500-still target |

## Generated artifacts (gitignored — large)

| File | Size guidance | Source |
|---|---|---|
| `data_inventory.csv` | one row per file across all configured paths/buckets | `scripts/phase0/audit_data.py` |
| `duplicates.csv` | one row per md5 with >1 occurrence | same |
| `quality_triage.csv` | one row per local PP video, with metrics | `scripts/phase0/quality_triage.py` |

## Reproducibility

```bash
# Always-on auth check
bash scripts/preflight_gcp.sh

# Pre-flight schema check
python scripts/preflight_check.py

# 0.1 — data audit (≈20 min — bottlenecks on GCS listing)
python scripts/phase0/audit_data.py

# 0.4 — tokenizer probe (deterministic, ~2s)
python scripts/phase0/tokenizer_probe.py

# After audit completes, the next three are all fast (read the CSV):
python scripts/phase0/resolution_fps.py
python scripts/phase0/anchor_inventory.py
python scripts/phase0/quality_triage.py
```

## Determinism

- `audit_data.py` sorts rows by source kind + path; two runs on identical filesystem state produce byte-identical CSVs.
- `tokenizer_probe.py` is deterministic (SentencePiece encoding) and the candidate list is fixed in source.
- `quality_triage.py` is deterministic given identical local files (OpenCV frame sampling at fixed indices, `volumedetect` output is reproducible).

## Known limitations

- **GCS quality triage is deferred.** `Phase0_QualityReport.md` covers local clips only. Triaging `video_gen_dataset` (~14TB) requires GPU-VM-side processing — Phase 1 work.
- **Watermark detection is SKIPPED.** Needs PP logo template crops in `pre_flight.yaml.phase0.pp_watermark_templates`. Followup task.
- **Local file state is volatile.** During Phase 0.1, the working tree was reorganized (folders moved into `lora_phase_v0/`). Re-run the audit if disk layout changes again before any decision relying on local clip counts.

## What landed

- Decisions made (with reasoning in the relevant report): brand token format (`CHASE_PP`/`SKYE_PP`); training resolution + FPS (960×544 @ 24 fps, 49/97 frames).
- Decisions awaiting user input: anchor-stills target split (proposal in §3.5); whether to fund GCS triage now or after Phase 1.

## What is explicitly NOT in Phase 0

- No Phase 1 (MLOps scaffolding) plan. That gets a fresh round once these reports are reviewed.
- No work on Steps 2–4 (audio, dialogue, script). Frozen.
- No training runs. Deferred until the visual baseline plan lands.
