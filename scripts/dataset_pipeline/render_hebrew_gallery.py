#!/usr/bin/env python3
"""Render a Hebrew-subtitled QA gallery.

Reads:
  <dataset>/processed_videos/0NNN.mp4  (the same letterboxed clips the
                                         model trains on)
  <dataset>/<captions_he_filename>     (index → Hebrew translation map)

Writes:
  <dataset>/other/gallery.mp4          (default — overrideable)

Layout: identical to training_gallery.mp4 (960×784, 49 frames per clip,
0.5s black filler, audio preserved). Caption is right-aligned in the
bottom band (Hebrew is RTL — ffmpeg drawtext + libfribidi handles BiDi).
Index #NNNN stays top-left, LTR.

Run on the VM (needs ffmpeg with libfribidi + a Hebrew-capable font;
DejaVu Sans Bold ships with Ubuntu and works).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "dataset_pipeline"))
from gallery import (  # noqa: E402
    AUDIO_BR,
    AUDIO_CH,
    AUDIO_CODEC,
    AUDIO_RATE,
    GALLERY_CLIP_DURATION_S,
    GALLERY_FPS,
    GALLERY_FRAMES_PER_CLIP,
    GALLERY_H,
    GALLERY_VIDEO_H,
    GALLERY_W,
    INDEX_FONTSIZE,
    PROMPT_FONTSIZE,
    _ensure_black_filler,
    build_concat_list,
    check_ffmpeg,
    concat_gallery,
)


# Hebrew tends to need slightly tighter wrapping (wider average glyph in
# DejaVu) and fewer cols to look balanced right-aligned.
HEBREW_WRAP_COLS = 60
HEBREW_MAX_LINES = 9


def wrap_hebrew(text: str) -> str:
    wrapped = textwrap.wrap(
        text.replace("\n", " ").strip(),
        width=HEBREW_WRAP_COLS,
        break_long_words=False,
        break_on_hyphens=False,
    )
    if len(wrapped) > HEBREW_MAX_LINES:
        wrapped = wrapped[:HEBREW_MAX_LINES]
        wrapped[-1] = wrapped[-1] + "…"
    return "\n".join(wrapped)


def find_hebrew_font() -> str:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/google-noto/NotoSansHebrew-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansHebrew-Bold.ttf",
    ]
    for p in candidates:
        if Path(p).exists():
            return p
    raise RuntimeError(
        "no Hebrew-capable bold font found in standard paths; install "
        "fonts-dejavu or fonts-noto-hebrew"
    )


def render_per_clip_he(src_mp4: Path, hebrew_text: str, idx: int, out_mp4: Path) -> None:
    """Same canvas + transform as training_gallery, but right-aligned
    Hebrew caption. Source is `processed_videos/` (already letterboxed
    + 49-frame trimmed) so no per-clip transform needed beyond canvas
    extension and the overlays."""
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    wrapped = wrap_hebrew(hebrew_text)
    prompt_tmp = out_mp4.with_suffix(".prompt_he.txt")
    prompt_tmp.write_text(wrapped, encoding="utf-8")

    font = find_hebrew_font()
    label = f"#{idx:04d}".replace(":", r"\:")  # safe for drawtext text=

    caption_y = GALLERY_VIDEO_H + 16

    vf = (
        # 1. canvas extension — bottom 240px black band (clip already 960×544)
        f"pad={GALLERY_W}:{GALLERY_H}:0:0:black,"
        # 2. index label — top-left, LTR (English)
        f"drawtext=text='{label}':x=24:y=24:"
        f"fontsize={INDEX_FONTSIZE}:fontcolor=white:"
        f"box=1:boxcolor=black@0.65:boxborderw=10:fontfile='{font}',"
        # 3. Hebrew caption — RIGHT-aligned (RTL natural alignment).
        #    text_align=right + libfribidi BiDi reorder happens automatically
        #    when text contains RTL codepoints. We position by computing
        #    x = (canvas_width - text_width - padding) per drawtext's
        #    text_w expression evaluated each frame.
        f"drawtext=textfile='{prompt_tmp}':"
        f"x=w-text_w-24:y={caption_y}:"
        f"fontsize={PROMPT_FONTSIZE}:fontcolor=white:"
        f"line_spacing=6:fontfile='{font}'"
    )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(src_mp4),
        "-vf", vf,
        "-r", str(GALLERY_FPS),
        "-frames:v", str(GALLERY_FRAMES_PER_CLIP),
        "-t", f"{GALLERY_CLIP_DURATION_S:.4f}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", AUDIO_CODEC, "-ar", str(AUDIO_RATE), "-ac", str(AUDIO_CH), "-b:a", AUDIO_BR,
        str(out_mp4),
    ]
    subprocess.run(cmd, check=True)
    prompt_tmp.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True, help="Dataset root dir on the VM")
    ap.add_argument("--captions", required=True, help="Path to captions_he.json (idx → Hebrew)")
    ap.add_argument("--out", default=None, help="Output mp4 (default: <dataset>/other/gallery.mp4)")
    args = ap.parse_args()

    check_ffmpeg()
    root = Path(args.dataset).resolve()
    md = json.loads((root / "metadata.json").read_text())
    captions = json.loads(Path(args.captions).read_text())

    out_path = Path(args.out) if args.out else (root / "other" / "gallery.mp4")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    work_dir = root / "_qa_render_cache" / "hebrew"
    per_clip_dir = work_dir / "per_clip"
    per_clip_dir.mkdir(parents=True, exist_ok=True)

    rendered = skipped = failed = 0
    print(f"=== Hebrew gallery for {root.name} ({len(md['clips'])} clip rows) ===", flush=True)

    for c in md["clips"]:
        if c.get("status", "active") != "active":
            continue
        if not c.get("download_ok", True) or not c.get("prompt"):
            continue
        idx = c["index"]
        he = captions.get(str(idx))
        if not he:
            print(f"  [{idx:04d}] no Hebrew caption — skipping", flush=True)
            failed += 1
            continue
        # Source is the letterboxed clip — same one training_gallery uses.
        src = root / "processed_videos" / f"{idx:04d}.mp4"
        if not src.exists():
            print(f"  [{idx:04d}] missing {src}", flush=True)
            failed += 1
            continue
        dst = per_clip_dir / f"{idx:04d}.mp4"
        if dst.exists() and dst.stat().st_size > 0:
            skipped += 1
            continue
        try:
            render_per_clip_he(src, he, idx, dst)
            rendered += 1
            if rendered % 50 == 0:
                print(f"  rendered={rendered} skipped={skipped} failed={failed}", flush=True)
        except subprocess.CalledProcessError as e:
            print(f"  [{idx:04d}] FAIL: {e}", flush=True)
            failed += 1

    print(f"\n[concat] rendered_total={rendered} skipped={skipped} failed={failed}", flush=True)
    active = [c["index"] for c in md["clips"]
              if c.get("status", "active") == "active"
              and c.get("download_ok", True)
              and c.get("prompt")
              and (per_clip_dir / f"{c['index']:04d}.mp4").exists()]
    concat_list = work_dir / "concat.txt"
    build_concat_list(active, per_clip_dir, concat_list)
    concat_gallery(concat_list, out_path)
    print(f"  → {out_path} ({out_path.stat().st_size/1024:.0f} KB, {len(active)} clips)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
