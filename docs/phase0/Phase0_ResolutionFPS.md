# Phase 0.3 — Resolution & FPS Analysis

Source: `docs/phase0/data_inventory.csv` (588613 total files, 1414 Paw-Patrol-relevant videos with width/height).

## Native resolution distribution (PP-relevant clips)

| Resolution (W×H) | Aspect | Count | Share |
|---|---|---|---|
| 768×1024 | 3:4 (0.750) | 589 | 41.7% |
| 1536×1024 | 4:3 (1.500) | 537 | 38.0% |
| 832×960 | 3:4 (0.867) | 68 | 4.8% |
| 960×544 | 16:9 (1.765) | 57 | 4.0% |
| 1920×544 | 16:9 (3.529) | 45 | 3.2% |
| 1280×672 | 16:9 (1.905) | 44 | 3.1% |
| 1664×960 | 16:9 (1.733) | 33 | 2.3% |
| 1280×720 | 16:9 (1.778) | 10 | 0.7% |
| 672×1280 | 9:16 (0.525) | 9 | 0.6% |
| 704×1280 | 9:16 (0.550) | 7 | 0.5% |
| 1948×1060 | 16:9 (1.838) | 6 | 0.4% |
| 1920×1080 | 16:9 (1.778) | 4 | 0.3% |
| 1950×1062 | 16:9 (1.836) | 4 | 0.3% |
| 1280×928 | 4:3 (1.379) | 1 | 0.1% |

## Native FPS distribution (PP-relevant clips)

| FPS bucket | Count | Share |
|---|---|---|
| 25 | 1228 | 86.8% |
| 24 | 181 | 12.8% |
| 30 | 4 | 0.3% |
| 23.37 | 1 | 0.1% |

## LTX-2 native training resolution + FPS

Cited from active configs in `packages/ltx-trainer/configs/`:

| Config | Resolution (W×H) | Frames | FPS | Use |
|---|---|---|---|---|
| `skye_exp2_standard.yaml:62` | 960×544 | 49 | 25 | Skye character baseline (most common) |
| `goldenPLUS150.yaml:73` | 960×544 | 49 | 24 | Multi-character combined |
| `full_cast_exp1_season1.yaml:79` | 1024×576 | 49 | 24 | 16:9 exact, full PP cast |
| `chase_exp1_highcap.yaml:80` | 960×832 | 49 | — | Portrait/wider for Chase |
| `skye_exp3_highres.yaml:72` | 768×1024 | 97 | 25 | Portrait, longer (4s) clip |
| `ltx2_av_lora.yaml:207` | 576×576 | — | — | Square benchmark |

**Constraints (architectural, not config-tunable):**

- VAE temporal compression: **8×**. Frame count must satisfy `frames % 8 == 1`. Source: [`packages/ltx-core/src/ltx_core/types.py:31`](../../packages/ltx-core/src/ltx_core/types.py).
- VAE spatial compression: **32×**. Width and height must be multiples of 32. Source: same file, line 19.
- Active frame buckets in production configs: **49** (~2s @ 24–25fps), **97** (~4s @ 24–25fps), occasionally **113**.
- Most-used training resolution across configs: **960×544 @ 24–25 fps with 49 frames** — this is the de facto LTX-2 training default for character LoRAs in this fork.

Repo notes: see `LTX_2.3_LoRA_Training_Analysis.md` (root) for the empirical write-up of these constraints.

## 9:16 vertical reframing — trade-offs

`active_plan §3.3` requires this analysis. The PP source clips are mostly 16:9 (TV broadcast). The Vertical Engine output is 9:16. Three approaches:

| Option | What it does | Pros | Cons |
|---|---|---|---|
| **Crop 16:9 → 9:16** | Center-crop horizontally, drop ~50%+ of frame width | Stays in trained resolution distribution. No new artifacts from synthesis. Dead simple. | **Loses 50%+ of brand-relevant horizontal content** (Adventure Bay sets, side-by-side characters, vehicle shots). |
| **Pillar-box (black bars)** | Render full 16:9 inside 9:16 frame with letterboxing | Preserves 100% of source content. | Wastes ~44% of pixels on black bars. Gemma's text prompts won't naturally describe "a video with black bars." |
| **Content-aware reframe** | Per-shot dynamic crop tracking subject | Looks native. Industry standard for vertical adaptation (e.g. After Effects' Auto-Reframe). | Adds a system we don't have. Risks introducing artifacts the LoRA might learn from. Hard to do consistently. |
| **Train at 9:16 directly** | Skip reframing — train on 9:16 source crops (no LTX-2 training precedent in this fork) | Native vertical output. | **LTX-2 has no 9:16 (≈0.5625) training precedent** — closest is 768×1024 (3:4 = 0.75). Off-distribution risk. |

**Note:** the existing PP source data resolution distribution above tells us how much loss each option costs, concretely.

## Recommendation

**Train at `960×544 @ 24 fps, 49 frames` (Static & Dynamic) and `97 frames` (Closeup) for the visual baseline.**
Reframing for 9:16 output happens at *inference / post* time, not at training time, via center-crop on the 960×544 output.

**Reasoning:**

1. **960×544 is LTX-2's empirical sweet spot.** It's the most-used training res in the existing configs, where the model has the cleanest learned distribution for character LoRAs. Off-distribution training (e.g. attempting native 9:16 at 576×1024) compounds two unknowns: catastrophic forgetting *and* novel resolution adaptation.
2. **24 fps over 25 fps** because (a) `goldenPLUS150.yaml` (the most-recent multi-character config) uses 24, and (b) 24 fps is the industry standard for animation broadcast — matching the source content's likely native rate. Both are within LTX-2's apparent FPS-agnostic range.
3. **Frame buckets 49 / 97 match `vision.md`'s 3-tier durations** (2–3s static, 3–5s dynamic). The 49-frame bucket also dominates production configs.
4. **Reframe at output time, not at training time.** The user-facing format (9:16) is a post-processing concern. Training resolution is a model-quality concern. Decoupling them lets us improve either independently. `vision.md` says 100% brand alignment is the gate, not vertical aspect ratio — vertical is a delivery format.

## What we'd give up if we chose differently

- **If we trained at native 9:16 (e.g. 576×1024)** — we'd accept LTX-2's untested low aspect-ratio behavior as a risk. Best case: cleaner verticals. Worst case: simultaneously fighting catastrophic forgetting *and* off-distribution drift, with no way to tell which is causing artifacts.
- **If we trained at 1024×576 (PP-exact 16:9)** — we'd be slightly off LTX-2's most-used training res, but with a small precedent in `full_cast_exp1_season1.yaml`. Slightly better source-fidelity at slightly lower model-fidelity. A defensible alternative; pick this if QC reveals 960×544 is dropping too much horizontal detail.
- **If we picked 25 fps instead of 24** — practically nothing. The Skye configs use 25; goldenPLUS150 uses 24. Symmetric choice.
- **If we used 113 frames (~4.7s)** — we'd be in slightly less-tested territory than 97; gain marginal duration. Not worth it.

## Open question for the user

9:16 reframing strategy is left for downstream work. Recommend revisiting after the visual baseline produces clips in 960×544 — the QC gallery will show concretely how much side-content is lost on a center-crop.
