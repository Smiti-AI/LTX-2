# Phase 0.5 — Anchor Stills Inventory & Gap Plan

Source: `docs/phase0/data_inventory.csv` (588613 total files, 316 PP-relevant images).

**Target (per `vision.md`):** 500 high-res stills anchoring rendering fidelity while motion is learned from video.

**"High-res" defined as:** shorter side ≥ 1024px.

## Have / target / gap matrix (high-res only)

| Character | Shot type | Have (high-res) | Target | Gap |
|---|---|---|---|---|
| chase | face | 88 | 70 | 0 |
| chase | paws | 0 | 40 | 40 |
| chase | full_body | 0 | 100 | 100 |
| chase | iconic | 60 | 40 | 0 |
| skye | face | 2 | 70 | 68 |
| skye | paws | 0 | 40 | 40 |
| skye | full_body | 0 | 100 | 100 |
| skye | iconic | 0 | 40 | 40 |
| **total** | — | **150** | **500** | **350** |

## Same matrix including non-high-res stills

(For reference — these contribute caption coverage but don't satisfy the high-res anchor criterion.)

| Character | Shot type | All images | High-res share |
|---|---|---|---|
| chase | face | 98 | 88/98 |
| chase | paws | 0 | 0/0 |
| chase | full_body | 0 | 0/0 |
| chase | iconic | 65 | 60/65 |
| skye | face | 2 | 2/2 |
| skye | paws | 0 | 0/0 |
| skye | full_body | 0 | 0/0 |
| skye | iconic | 0 | 0/0 |

**Uncategorized PP images** (no shot-type keyword match): 151 (110 Chase, 41 Skye). These need manual review or richer heuristics before they can be assigned to a bucket.

## Initial target split (subject to user sign-off)

vision.md specifies 500 stills total but doesn't break them down. Proposed split, sized so face + full_body dominate (these drive the catastrophic-forgetting failure modes most strongly per the vision doc), with paws and iconic-pose anchors as a smaller floor:

| Character | face | paws | full_body | iconic | Total |
|---|---|---|---|---|---|
| chase | 70 | 40 | 100 | 40 | 250 |
| skye | 70 | 40 | 100 | 40 | 250 |

Sums to 500. Adjust freely — the matrix above will recompute automatically.

## Recommended extraction methodology

To close the gap, extract additional anchor stills from S-tier source clips:

1. **Source:** the top-decile clips from `Phase0_QualityReport.md` (high sharpness, no letterboxing, no watermark).
2. **Method:** keyframe extraction at scene-change boundaries (`ffmpeg -vf select='gt(scene\,0.4)'`) plus uniform sampling every N frames as a fallback.
3. **Filter:** automatic crop check — discard frames with ≥ 5% bands of pure black/letterbox edges. Manual visual pass on the survivors.
4. **Bucket assignment:** filename heuristic first (this script), human label for the residual uncategorized set.
5. **Storage:** new stills at native source resolution (no upscaling — see pushback below). Filename convention `{character}_{shot_type}_{source_episode}_{frame_index}.png`.

Becomes a Phase 2 sub-task per `active_plan.md §3.5` after the gap is signed off.

## Pushback (for user to weigh)

- **Skip upscaling.** vision.md mentions "optional upscaling pipeline." Recommend not upscaling: super-resolution models hallucinate fine detail (eyes, badges, fur) which is *exactly* the catastrophic-forgetting territory we're fighting. If extraction can't reach 500 from native-res frames, lower the high-res threshold or accept a smaller anchor set rather than synthesize fake detail.
- **Re-tune the resolution threshold (1024px).** This is currently a floor. If the histogram in `Phase0_QualityReport.md` shows the source distribution clusters below 1024, drop to 720 (or whatever the source actually provides). The gate is "better than the LoRA's output resolution," not an absolute number.
- **Shot-type heuristic gaps.** The keyword-based bucketing will miss frames where the shot type isn't named in the path. Once the audit is done, scan `uncategorized` count — if it's > 30% of PP images, the heuristic needs improvement (or a one-time human pass).
