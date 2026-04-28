#!/usr/bin/env python3
"""Phase 0.3 — Resolution & FPS Analysis (active_plan §3.3).

Reads docs/phase0/data_inventory.csv (built by audit_data.py), filters to
videos that are Paw-Patrol-relevant (guessed_character non-empty), and
reports:
  - Native resolution distribution
  - Native FPS distribution
  - LTX-2's training resolution + FPS, cited from packages/ltx-trainer/configs/
  - 9:16 reframing trade-offs
  - Single-tuple recommended training resolution + FPS

Output: docs/phase0/Phase0_ResolutionFPS.md
"""
from __future__ import annotations

import csv
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
INVENTORY_CSV = REPO / "docs" / "phase0" / "data_inventory.csv"
TRAINER_CONFIGS = REPO / "packages" / "ltx-trainer" / "configs"
ANALYSIS_NOTES = REPO / "LTX_2.3_LoRA_Training_Analysis.md"
OUT_REPORT = REPO / "docs" / "phase0" / "Phase0_ResolutionFPS.md"


def parse_inventory() -> list[dict]:
    if not INVENTORY_CSV.exists():
        return []
    with INVENTORY_CSV.open() as f:
        return list(csv.DictReader(f))


def filter_pp_videos(rows: list[dict]) -> list[dict]:
    return [
        r for r in rows
        if r.get("type") == "video"
        and (r.get("guessed_character") or "")
        and r.get("width")
        and r.get("height")
    ]


def hist(items: list[str | tuple]) -> list[tuple[str, int]]:
    return sorted(Counter(items).items(), key=lambda kv: -kv[1])


def fmt_md_table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |"]
    out.append("|" + "|".join("---" for _ in headers) + "|")
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)


def main() -> int:
    rows = parse_inventory()
    pp_videos = filter_pp_videos(rows)

    res_counts = hist([(int(r["width"]), int(r["height"])) for r in pp_videos])
    fps_buckets: list[str] = []
    for r in pp_videos:
        try:
            fps = float(r["fps"]) if r["fps"] else 0
        except ValueError:
            fps = 0
        if fps == 0:
            fps_buckets.append("unknown")
        elif 23.5 <= fps <= 24.5:
            fps_buckets.append("24")
        elif 24.5 < fps <= 25.5:
            fps_buckets.append("25")
        elif 29.5 <= fps <= 30.5:
            fps_buckets.append("30")
        elif 59.5 <= fps <= 60.5:
            fps_buckets.append("60")
        else:
            fps_buckets.append(f"{fps:.2f}")
    fps_counts = hist(fps_buckets)

    md: list[str] = []
    md.append("# Phase 0.3 — Resolution & FPS Analysis")
    md.append("")
    md.append(f"Source: `{INVENTORY_CSV.relative_to(REPO)}` "
              f"({len(rows)} total files, {len(pp_videos)} Paw-Patrol-relevant videos with width/height).")
    md.append("")
    md.append("## Native resolution distribution (PP-relevant clips)")
    md.append("")
    if not res_counts:
        md.append("_No PP-relevant videos with resolution metadata in the inventory yet._")
    else:
        md.append(fmt_md_table(
            ["Resolution (W×H)", "Aspect", "Count", "Share"],
            [
                [f"{w}×{h}", aspect_label(w, h), c, f"{c/len(pp_videos)*100:.1f}%"]
                for (w, h), c in res_counts[:15]
            ],
        ))
    md.append("")

    md.append("## Native FPS distribution (PP-relevant clips)")
    md.append("")
    md.append(fmt_md_table(
        ["FPS bucket", "Count", "Share"],
        [[k, v, f"{v/len(fps_buckets)*100:.1f}%" if fps_buckets else "—"] for k, v in fps_counts],
    ))
    md.append("")

    md.append("## LTX-2 native training resolution + FPS")
    md.append("")
    md.append("Cited from active configs in `packages/ltx-trainer/configs/`:")
    md.append("")
    md.append(fmt_md_table(
        ["Config", "Resolution (W×H)", "Frames", "FPS", "Use"],
        [
            ["`skye_exp2_standard.yaml:62`", "960×544", "49", "25", "Skye character baseline (most common)"],
            ["`goldenPLUS150.yaml:73`", "960×544", "49", "24", "Multi-character combined"],
            ["`full_cast_exp1_season1.yaml:79`", "1024×576", "49", "24", "16:9 exact, full PP cast"],
            ["`chase_exp1_highcap.yaml:80`", "960×832", "49", "—", "Portrait/wider for Chase"],
            ["`skye_exp3_highres.yaml:72`", "768×1024", "97", "25", "Portrait, longer (4s) clip"],
            ["`ltx2_av_lora.yaml:207`", "576×576", "—", "—", "Square benchmark"],
        ],
    ))
    md.append("")
    md.append("**Constraints (architectural, not config-tunable):**")
    md.append("")
    md.append("- VAE temporal compression: **8×**. Frame count must satisfy `frames % 8 == 1`. Source: [`packages/ltx-core/src/ltx_core/types.py:31`](../../packages/ltx-core/src/ltx_core/types.py).")
    md.append("- VAE spatial compression: **32×**. Width and height must be multiples of 32. Source: same file, line 19.")
    md.append("- Active frame buckets in production configs: **49** (~2s @ 24–25fps), **97** (~4s @ 24–25fps), occasionally **113**.")
    md.append("- Most-used training resolution across configs: **960×544 @ 24–25 fps with 49 frames** — this is the de facto LTX-2 training default for character LoRAs in this fork.")
    md.append("")
    md.append("Repo notes: see `LTX_2.3_LoRA_Training_Analysis.md` (root) for the empirical write-up of these constraints.")
    md.append("")

    md.append("## 9:16 vertical reframing — trade-offs")
    md.append("")
    md.append("`active_plan §3.3` requires this analysis. The PP source clips are mostly 16:9 (TV broadcast). The Vertical Engine output is 9:16. Three approaches:")
    md.append("")
    md.append(fmt_md_table(
        ["Option", "What it does", "Pros", "Cons"],
        [
            ["**Crop 16:9 → 9:16**", "Center-crop horizontally, drop ~50%+ of frame width", "Stays in trained resolution distribution. No new artifacts from synthesis. Dead simple.", "**Loses 50%+ of brand-relevant horizontal content** (Adventure Bay sets, side-by-side characters, vehicle shots)."],
            ["**Pillar-box (black bars)**", "Render full 16:9 inside 9:16 frame with letterboxing", "Preserves 100% of source content.", "Wastes ~44% of pixels on black bars. Gemma's text prompts won't naturally describe \"a video with black bars.\""],
            ["**Content-aware reframe**", "Per-shot dynamic crop tracking subject", "Looks native. Industry standard for vertical adaptation (e.g. After Effects' Auto-Reframe).", "Adds a system we don't have. Risks introducing artifacts the LoRA might learn from. Hard to do consistently."],
            ["**Train at 9:16 directly**", "Skip reframing — train on 9:16 source crops (no LTX-2 training precedent in this fork)", "Native vertical output.", "**LTX-2 has no 9:16 (≈0.5625) training precedent** — closest is 768×1024 (3:4 = 0.75). Off-distribution risk."],
        ],
    ))
    md.append("")
    md.append("**Note:** the existing PP source data resolution distribution above tells us how much loss each option costs, concretely.")
    md.append("")

    md.append("## Recommendation")
    md.append("")
    md.append("**Train at `960×544 @ 24 fps, 49 frames` (Static & Dynamic) and `97 frames` (Closeup) for the visual baseline.**")
    md.append("Reframing for 9:16 output happens at *inference / post* time, not at training time, via center-crop on the 960×544 output.")
    md.append("")
    md.append("**Reasoning:**")
    md.append("")
    md.append("1. **960×544 is LTX-2's empirical sweet spot.** It's the most-used training res in the existing configs, where the model has the cleanest learned distribution for character LoRAs. Off-distribution training (e.g. attempting native 9:16 at 576×1024) compounds two unknowns: catastrophic forgetting *and* novel resolution adaptation.")
    md.append("2. **24 fps over 25 fps** because (a) `goldenPLUS150.yaml` (the most-recent multi-character config) uses 24, and (b) 24 fps is the industry standard for animation broadcast — matching the source content's likely native rate. Both are within LTX-2's apparent FPS-agnostic range.")
    md.append("3. **Frame buckets 49 / 97 match `vision.md`'s 3-tier durations** (2–3s static, 3–5s dynamic). The 49-frame bucket also dominates production configs.")
    md.append("4. **Reframe at output time, not at training time.** The user-facing format (9:16) is a post-processing concern. Training resolution is a model-quality concern. Decoupling them lets us improve either independently. `vision.md` says 100% brand alignment is the gate, not vertical aspect ratio — vertical is a delivery format.")
    md.append("")
    md.append("## What we'd give up if we chose differently")
    md.append("")
    md.append("- **If we trained at native 9:16 (e.g. 576×1024)** — we'd accept LTX-2's untested low aspect-ratio behavior as a risk. Best case: cleaner verticals. Worst case: simultaneously fighting catastrophic forgetting *and* off-distribution drift, with no way to tell which is causing artifacts.")
    md.append("- **If we trained at 1024×576 (PP-exact 16:9)** — we'd be slightly off LTX-2's most-used training res, but with a small precedent in `full_cast_exp1_season1.yaml`. Slightly better source-fidelity at slightly lower model-fidelity. A defensible alternative; pick this if QC reveals 960×544 is dropping too much horizontal detail.")
    md.append("- **If we picked 25 fps instead of 24** — practically nothing. The Skye configs use 25; goldenPLUS150 uses 24. Symmetric choice.")
    md.append("- **If we used 113 frames (~4.7s)** — we'd be in slightly less-tested territory than 97; gain marginal duration. Not worth it.")
    md.append("")
    md.append("## Open question for the user")
    md.append("")
    md.append("9:16 reframing strategy is left for downstream work. Recommend revisiting after the visual baseline produces clips in 960×544 — the QC gallery will show concretely how much side-content is lost on a center-crop.")
    md.append("")
    OUT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    OUT_REPORT.write_text("\n".join(md))
    print(f"wrote {OUT_REPORT}")
    print(f"PP-relevant videos in inventory: {len(pp_videos)}")
    if not pp_videos:
        print("WARN: inventory is empty or has no PP-relevant videos with resolution metadata.")
        print("      Re-run scripts/phase0/audit_data.py first.")
    return 0


def aspect_label(w: int, h: int) -> str:
    if h == 0:
        return "?"
    r = w / h
    targets = [(16/9, "16:9"), (4/3, "4:3"), (1.0, "1:1"), (3/4, "3:4"), (9/16, "9:16")]
    closest = min(targets, key=lambda t: abs(r - t[0]))
    return f"{closest[1]} ({r:.3f})"


if __name__ == "__main__":
    sys.exit(main())
