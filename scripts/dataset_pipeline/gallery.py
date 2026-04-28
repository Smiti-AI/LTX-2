"""Gallery video render — two-pass ffmpeg per `docs/training_data_pipeline.md` §4.

Usage as a library:
    from gallery import render_per_clip, build_concat_list, concat_gallery
    render_per_clip(src_mp4, prompt_text, idx, out_mp4)
    build_concat_list(active_indices, work_dir, list_path)
    concat_gallery(list_path, out_mp4)
"""
from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

GALLERY_W = 960
GALLERY_H = 544
GALLERY_FPS = 24
INTERCLIP_BLACK_S = 0.5
PROMPT_WRAP_COLS = 48
PROMPT_MAX_LINES = 4
INDEX_FONTSIZE = 56
PROMPT_FONTSIZE = 26


def _find_font() -> str:
    """Return an ffmpeg-readable absolute path to a font file. Tries several
    common locations; returns "" if none found (drawtext then uses ffmpeg's
    default search, which usually works on Linux but is unreliable on macOS)."""
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",  # macOS
        "/System/Library/Fonts/Helvetica.ttc",                 # macOS
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Debian/Ubuntu
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",          # Fedora/CentOS
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/google-noto/NotoSans-Bold.ttf",
    ]
    for p in candidates:
        if Path(p).exists():
            return p
    return ""


def _wrap_caption(text: str) -> str:
    # Word-wrap at PROMPT_WRAP_COLS; truncate to PROMPT_MAX_LINES with ellipsis.
    wrapped = textwrap.wrap(text.replace("\n", " ").strip(), width=PROMPT_WRAP_COLS,
                            break_long_words=False, break_on_hyphens=False)
    if len(wrapped) > PROMPT_MAX_LINES:
        wrapped = wrapped[:PROMPT_MAX_LINES]
        wrapped[-1] = wrapped[-1].rstrip(".,;:") + "…"
    return "\n".join(wrapped)


def _drawtext_escape(s: str) -> str:
    # ffmpeg drawtext text= needs : ' \ % escaped; we use textfile= instead so
    # this is only used for the index label which is a fixed short string.
    return s.replace("\\", r"\\").replace(":", r"\:").replace("'", r"\'")


def _ensure_black_filler(work_dir: Path) -> Path:
    p = work_dir / "_black.mp4"
    if p.exists():
        return p
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi",
        "-i", f"color=c=black:s={GALLERY_W}x{GALLERY_H}:d={INTERCLIP_BLACK_S}:r={GALLERY_FPS}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-an",
        str(p),
    ]
    subprocess.run(cmd, check=True)
    return p


def render_per_clip(src_mp4: Path, prompt_text: str, idx: int, out_mp4: Path) -> None:
    """Pass 1: scale + pad to canvas, burn index top-left, burn caption bottom."""
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    wrapped = _wrap_caption(prompt_text)
    # Write the wrapped prompt to a sidecar text file for drawtext textfile=.
    prompt_tmp = out_mp4.with_suffix(".prompt.txt")
    prompt_tmp.write_text(wrapped)

    font = _find_font()
    font_arg = f":fontfile='{font}'" if font else ""

    label = f"#{idx:04d}"
    label_esc = _drawtext_escape(label)

    vf = (
        f"scale={GALLERY_W}:{GALLERY_H}:force_original_aspect_ratio=decrease,"
        f"pad={GALLERY_W}:{GALLERY_H}:(ow-iw)/2:(oh-ih)/2:black,"
        f"drawtext=text='{label_esc}':x=24:y=24:"
        f"fontsize={INDEX_FONTSIZE}:fontcolor=white:"
        f"box=1:boxcolor=black@0.65:boxborderw=12{font_arg},"
        f"drawtext=textfile='{prompt_tmp}':"
        f"x=(w-text_w)/2:y=h-text_h-30:"
        f"fontsize={PROMPT_FONTSIZE}:fontcolor=white:"
        f"box=1:boxcolor=black@0.7:boxborderw=14:line_spacing=8{font_arg}"
    )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(src_mp4),
        "-vf", vf,
        "-r", str(GALLERY_FPS),
        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-an",
        str(out_mp4),
    ]
    subprocess.run(cmd, check=True)
    prompt_tmp.unlink(missing_ok=True)


def build_concat_list(active_indices: list[int], per_clip_dir: Path, list_path: Path) -> None:
    """Produce a concat-demuxer list interleaving black filler between clips.
    Skips clips whose per-clip render is missing (e.g. failed during pass 1)."""
    black = _ensure_black_filler(per_clip_dir)
    lines: list[str] = []
    first = True
    for idx in active_indices:
        clip = per_clip_dir / f"{idx:04d}.mp4"
        if not clip.exists():
            continue
        if not first:
            lines.append(f"file '{black.resolve()}'")
        lines.append(f"file '{clip.resolve()}'")
        first = False
    list_path.write_text("\n".join(lines) + "\n")


def concat_gallery(list_path: Path, out_mp4: Path) -> None:
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0",
        "-i", str(list_path),
        "-c", "copy",
        str(out_mp4),
    ]
    subprocess.run(cmd, check=True)


def check_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise RuntimeError("ffmpeg/ffprobe not on PATH. Install ffmpeg first.")
