#!/usr/bin/env python3
"""Phase 0.5 — Anchor Stills Inventory & Gap Plan (active_plan §3.5).

Reads docs/phase0/data_inventory.csv. Filters to images that are
Paw-Patrol-relevant. Buckets by (character × shot type) to compute the gap
to the 500 high-res anchor stills target from vision.md.

Output: docs/phase0/Phase0_AnchorGap.md
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Iterable

REPO = Path(__file__).resolve().parent.parent.parent
INVENTORY_CSV = REPO / "docs" / "phase0" / "data_inventory.csv"
OUT_REPORT = REPO / "docs" / "phase0" / "Phase0_AnchorGap.md"

# Initial proposed target split. Sums to 500 per vision.md.
# User signs off on this before any extraction work begins.
TARGET = {
    ("chase", "face"): 70,
    ("chase", "paws"): 40,
    ("chase", "full_body"): 100,
    ("chase", "iconic"): 40,
    ("skye", "face"): 70,
    ("skye", "paws"): 40,
    ("skye", "full_body"): 100,
    ("skye", "iconic"): 40,
}
SHOT_TYPES = ("face", "paws", "full_body", "iconic")
CHARACTERS = ("chase", "skye")

# Heuristic shot-type keywords (path/filename substrings).
SHOT_KEYWORDS = {
    "face": ["face", "head", "closeup", "close_up", "portrait", "eye", "snout"],
    "paws": ["paw", "paws"],
    "full_body": ["full_body", "fullbody", "full-body", "whole_body", "wholebody"],
    "iconic": ["march", "bubble", "sneeze", "backflip", "pose", "flying", "lift"],
}

# Resolution threshold for "high-res anchor". 1024px shorter side = the floor.
HIGH_RES_MIN_SHORTER_SIDE = 1024


def parse_inventory() -> list[dict]:
    if not INVENTORY_CSV.exists():
        return []
    with INVENTORY_CSV.open() as f:
        return list(csv.DictReader(f))


def get_int(s: str) -> int:
    try:
        return int(s)
    except (ValueError, TypeError):
        return 0


def is_pp_image(r: dict) -> bool:
    if r.get("type") != "image":
        return False
    char = r.get("guessed_character", "") or ""
    return any(c in char.split(",") for c in CHARACTERS)


def primary_character(r: dict) -> str:
    chars = (r.get("guessed_character") or "").split(",")
    for c in CHARACTERS:
        if c in chars:
            return c
    return ""


def shot_type(path: str) -> str:
    p = path.lower()
    for label, kws in SHOT_KEYWORDS.items():
        if any(k in p for k in kws):
            return label
    return "uncategorized"


def is_high_res(r: dict) -> bool:
    w, h = get_int(r.get("width", "")), get_int(r.get("height", ""))
    if w == 0 or h == 0:
        return False
    return min(w, h) >= HIGH_RES_MIN_SHORTER_SIDE


def fmt_table(headers: Iterable[str], rows: Iterable[Iterable]) -> str:
    headers = list(headers)
    out = ["| " + " | ".join(headers) + " |"]
    out.append("|" + "|".join("---" for _ in headers) + "|")
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)


def main() -> int:
    rows = parse_inventory()
    images = [r for r in rows if is_pp_image(r)]

    # have_total[char][shot] = count of all images
    # have_hires[char][shot] = count of high-res images
    have_total = {(c, s): 0 for c in CHARACTERS for s in SHOT_TYPES}
    have_hires = {(c, s): 0 for c in CHARACTERS for s in SHOT_TYPES}
    have_uncategorized = {c: 0 for c in CHARACTERS}

    for r in images:
        c = primary_character(r)
        if not c:
            continue
        s = shot_type(r.get("path", ""))
        if s == "uncategorized":
            have_uncategorized[c] += 1
            continue
        have_total[(c, s)] += 1
        if is_high_res(r):
            have_hires[(c, s)] += 1

    md: list[str] = []
    md.append("# Phase 0.5 — Anchor Stills Inventory & Gap Plan")
    md.append("")
    md.append(
        f"Source: `{INVENTORY_CSV.relative_to(REPO)}` "
        f"({len(rows)} total files, {len(images)} PP-relevant images)."
    )
    md.append("")
    md.append("**Target (per `vision.md`):** 500 high-res stills anchoring rendering fidelity while motion is learned from video.")
    md.append("")
    md.append(f"**\"High-res\" defined as:** shorter side ≥ {HIGH_RES_MIN_SHORTER_SIDE}px.")
    md.append("")

    md.append("## Have / target / gap matrix (high-res only)")
    md.append("")
    table_rows = []
    total_have = 0
    total_target = 0
    for c in CHARACTERS:
        for s in SHOT_TYPES:
            have = have_hires[(c, s)]
            tgt = TARGET[(c, s)]
            gap = max(0, tgt - have)
            total_have += have
            total_target += tgt
            table_rows.append([c, s, have, tgt, gap])
    table_rows.append(["**total**", "—", f"**{total_have}**", f"**{total_target}**", f"**{max(0, total_target - total_have)}**"])
    md.append(fmt_table(["Character", "Shot type", "Have (high-res)", "Target", "Gap"], table_rows))
    md.append("")

    md.append("## Same matrix including non-high-res stills")
    md.append("")
    md.append("(For reference — these contribute caption coverage but don't satisfy the high-res anchor criterion.)")
    md.append("")
    md.append(fmt_table(
        ["Character", "Shot type", "All images", "High-res share"],
        [
            [
                c, s, have_total[(c, s)],
                f"{have_hires[(c, s)]}/{have_total[(c, s)]}" if have_total[(c, s)] else "0/0",
            ]
            for c in CHARACTERS for s in SHOT_TYPES
        ],
    ))
    md.append("")
    md.append(f"**Uncategorized PP images** (no shot-type keyword match): {sum(have_uncategorized.values())} "
              f"({have_uncategorized.get('chase', 0)} Chase, {have_uncategorized.get('skye', 0)} Skye). "
              "These need manual review or richer heuristics before they can be assigned to a bucket.")
    md.append("")

    md.append("## Initial target split (subject to user sign-off)")
    md.append("")
    md.append(
        "vision.md specifies 500 stills total but doesn't break them down. "
        "Proposed split, sized so face + full_body dominate (these drive the catastrophic-forgetting "
        "failure modes most strongly per the vision doc), with paws and iconic-pose anchors as a smaller floor:"
    )
    md.append("")
    md.append(fmt_table(
        ["Character", "face", "paws", "full_body", "iconic", "Total"],
        [
            [c] + [TARGET[(c, s)] for s in SHOT_TYPES] + [sum(TARGET[(c, s)] for s in SHOT_TYPES)]
            for c in CHARACTERS
        ],
    ))
    md.append("")
    md.append("Sums to 500. Adjust freely — the matrix above will recompute automatically.")
    md.append("")

    md.append("## Recommended extraction methodology")
    md.append("")
    md.append("To close the gap, extract additional anchor stills from S-tier source clips:")
    md.append("")
    md.append("1. **Source:** the top-decile clips from `Phase0_QualityReport.md` (high sharpness, no letterboxing, no watermark).")
    md.append("2. **Method:** keyframe extraction at scene-change boundaries (`ffmpeg -vf select='gt(scene\\,0.4)'`) plus uniform sampling every N frames as a fallback.")
    md.append("3. **Filter:** automatic crop check — discard frames with ≥ 5% bands of pure black/letterbox edges. Manual visual pass on the survivors.")
    md.append("4. **Bucket assignment:** filename heuristic first (this script), human label for the residual uncategorized set.")
    md.append("5. **Storage:** new stills at native source resolution (no upscaling — see pushback below). Filename convention `{character}_{shot_type}_{source_episode}_{frame_index}.png`.")
    md.append("")
    md.append("Becomes a Phase 2 sub-task per `active_plan.md §3.5` after the gap is signed off.")
    md.append("")

    md.append("## Pushback (for user to weigh)")
    md.append("")
    md.append("- **Skip upscaling.** vision.md mentions \"optional upscaling pipeline.\" Recommend not upscaling: super-resolution models hallucinate fine detail (eyes, badges, fur) which is *exactly* the catastrophic-forgetting territory we're fighting. If extraction can't reach 500 from native-res frames, lower the high-res threshold or accept a smaller anchor set rather than synthesize fake detail.")
    md.append(f"- **Re-tune the resolution threshold ({HIGH_RES_MIN_SHORTER_SIDE}px).** This is currently a floor. If the histogram in `Phase0_QualityReport.md` shows the source distribution clusters below 1024, drop to 720 (or whatever the source actually provides). The gate is \"better than the LoRA's output resolution,\" not an absolute number.")
    md.append("- **Shot-type heuristic gaps.** The keyword-based bucketing will miss frames where the shot type isn't named in the path. Once the audit is done, scan `uncategorized` count — if it's > 30% of PP images, the heuristic needs improvement (or a one-time human pass).")
    md.append("")
    OUT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    OUT_REPORT.write_text("\n".join(md))
    print(f"wrote {OUT_REPORT}")
    print(f"PP-relevant images in inventory: {len(images)}")
    if not images:
        print("WARN: inventory has no PP-relevant images. Re-run audit_data.py first.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
