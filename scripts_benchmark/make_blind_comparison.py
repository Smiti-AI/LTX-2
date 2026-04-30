"""Blind 3-way side-by-side comparison videos.

For each scene shared by three folders (base, methodA, methodB), build a single
output video that:

  Loop 1 — all three play together, muted.
  Loop 2 — left audio only.
  Loop 3 — middle audio only.
  Loop 4 — right audio only.
  Loop 5 — titles revealed, muted.

Left/middle/right placement is randomized per scene (mapping saved to
_mapping.json so the answer key exists). Scene description is burned in as a
caption banner at the bottom of every loop.

Text is rendered to PNG via PIL and overlaid (this ffmpeg build has no drawtext).

Usage:
    python3 make_blind_comparison.py
"""

from __future__ import annotations

import json
import random
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path("/Users/efrattaig/projects/sm/LTX-2/lora_results/goldenPlus150_version1")
OUT_DIR = ROOT / "Comparison"
TMP_DIR = OUT_DIR / "_overlays"
SCENES_ROOT = Path(
    "/Users/efrattaig/projects/sm/LTX-2/inputs/benchmarks/BM_v2_camera_movement/benchmark_v1"
)
FONT = "/System/Library/Fonts/Supplemental/Arial.ttf"
FONT_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"

SOURCES: list[tuple[str, Path, str]] = [
    # User pointed at goldenPLUS150_benchmark/_base, but those clips are 4s
    # while the LoRA outputs are 9.7s. The _base inside the run folders has
    # matching 9.7s base clips — using that for duration parity (both run
    # folders' _base files are byte-identical).
    ("base", ROOT / "goldenPLUS150_v2_rank128_bm" / "_base", "{scene}.mp4"),
    ("rank32_scratch", ROOT / "goldenPLUS150_v2_rank32_scratch" / "step_10000", "{scene}_lora.mp4"),
    ("rank128_bm", ROOT / "goldenPLUS150_v2_rank128_bm" / "step_10000", "{scene}_lora.mp4"),
]

CLIP_HEIGHT = 540
SEG_DUR = 9.708333


@dataclass
class Clip:
    label: str
    path: Path


def find_scenes() -> list[str]:
    sets = []
    for label, folder, pattern in SOURCES:
        names = set()
        for f in folder.iterdir():
            if f.suffix != ".mp4":
                continue
            stem = f.stem
            if pattern.endswith("_lora.mp4"):
                if stem.endswith("_lora"):
                    names.add(stem[: -len("_lora")])
            else:
                names.add(stem)
        sets.append(names)
    return sorted(set.intersection(*sets))


def load_caption(scene: str) -> str:
    cfg = SCENES_ROOT / scene / "config.json"
    if not cfg.exists():
        return scene
    return (json.loads(cfg.read_text()).get("description") or scene).strip()


def render_caption_png(text: str, width: int, out_path: Path) -> tuple[int, int]:
    font = ImageFont.truetype(FONT, 22)
    pad = 16
    max_text_w = width - 2 * pad - 24
    # Wrap by pixels
    lines = []
    for paragraph in text.split("\n"):
        cur = ""
        for word in paragraph.split():
            trial = (cur + " " + word).strip()
            w = font.getlength(trial)
            if w <= max_text_w:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
    line_h = font.size + 6
    text_h = line_h * len(lines)
    h = text_h + 2 * pad
    img = Image.new("RGBA", (width, h), (0, 0, 0, 160))
    draw = ImageDraw.Draw(img)
    y = pad
    for line in lines:
        tw = font.getlength(line)
        x = (width - tw) // 2
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
        y += line_h
    img.save(out_path)
    return width, h


def render_title_png(text: str, out_path: Path) -> tuple[int, int]:
    try:
        font = ImageFont.truetype(FONT_BOLD, 34)
    except OSError:
        font = ImageFont.truetype(FONT, 34)
    pad_x, pad_y = 18, 10
    tw = int(font.getlength(text))
    th = font.size + 8
    w = tw + 2 * pad_x
    h = th + 2 * pad_y
    img = Image.new("RGBA", (w, h), (0, 0, 0, 200))
    draw = ImageDraw.Draw(img)
    draw.text((pad_x, pad_y), text, font=font, fill=(255, 230, 70, 255))
    img.save(out_path)
    return w, h


def probe_size(path: Path) -> tuple[int, int]:
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-of", "csv=p=0",
        "-show_entries", "stream=width,height",
        "-select_streams", "v:0", str(path),
    ]).decode().strip()
    w, h = out.split(",")
    return int(w), int(h)


def even(n: int) -> int:
    return n - (n % 2)


def build_filter(per_clip_w: int, hstack_w: int, cap_h: int) -> str:
    H = CLIP_HEIGHT
    BORDER = 10  # green border thickness, px
    GREEN = "0x00FF00"
    parts = [
        f"[0:v]scale={per_clip_w}:{H},setsar=1,fps=24,trim=0:{SEG_DUR},setpts=PTS-STARTPTS[lv0]",
        f"[1:v]scale={per_clip_w}:{H},setsar=1,fps=24,trim=0:{SEG_DUR},setpts=PTS-STARTPTS[mv0]",
        f"[2:v]scale={per_clip_w}:{H},setsar=1,fps=24,trim=0:{SEG_DUR},setpts=PTS-STARTPTS[rv0]",
        # Split each into two: plain (loops 1-4) and titled (loop 5)
        "[lv0]split=2[lvA][lvB]",
        "[mv0]split=2[mvA][mvB]",
        "[rv0]split=2[rvA][rvB]",
        # Plain hstack
        "[lvA][mvA][rvA]hstack=inputs=3[plain]",
        # Caption overlay (input 3)
        "[plain][3:v]overlay=x=(W-w)/2:y=H-h-12[plain_cap]",
        # Split into 4: loop1 (no highlight), loop2 (left), loop3 (mid), loop4 (right)
        "[plain_cap]split=4[p1][p2_pre][p3_pre][p4_pre]",
        # Green border on the active clip's region for loops 2/3/4
        f"[p2_pre]drawbox=x=0:y=0:w={per_clip_w}:h={H}:color={GREEN}:t={BORDER}[p2]",
        f"[p3_pre]drawbox=x={per_clip_w}:y=0:w={per_clip_w}:h={H}:color={GREEN}:t={BORDER}[p3]",
        f"[p4_pre]drawbox=x={2*per_clip_w}:y=0:w={per_clip_w}:h={H}:color={GREEN}:t={BORDER}[p4]",
        # Titled hstack (inputs 4,5,6 = title PNGs)
        "[lvB][4:v]overlay=x=(W-w)/2:y=24[lvT]",
        "[mvB][5:v]overlay=x=(W-w)/2:y=24[mvT]",
        "[rvB][6:v]overlay=x=(W-w)/2:y=24[rvT]",
        "[lvT][mvT][rvT]hstack=inputs=3[titled]",
        "[titled][3:v]overlay=x=(W-w)/2:y=H-h-12[p5]",
        # Audio
        f"[0:a]apad,atrim=0:{SEG_DUR},asetpts=PTS-STARTPTS[la]",
        f"[1:a]apad,atrim=0:{SEG_DUR},asetpts=PTS-STARTPTS[ma]",
        f"[2:a]apad,atrim=0:{SEG_DUR},asetpts=PTS-STARTPTS[ra]",
        f"anullsrc=channel_layout=stereo:sample_rate=48000:d={SEG_DUR}[s1]",
        f"anullsrc=channel_layout=stereo:sample_rate=48000:d={SEG_DUR}[s5]",
        "[p1][s1][p2][la][p3][ma][p4][ra][p5][s5]concat=n=5:v=1:a=1[outv][outa]",
    ]
    return ";".join(parts)


def render_scene(scene: str, clips: list[Clip], caption: str, out_path: Path) -> None:
    src_w, src_h = probe_size(clips[0].path)
    per_clip_w = even(int(round(src_w * (CLIP_HEIGHT / src_h))))
    hstack_w = per_clip_w * 3

    cap_png = TMP_DIR / f"{scene}__cap.png"
    _, cap_h = render_caption_png(caption, hstack_w - 40, cap_png)

    title_pngs = []
    for i, c in enumerate(clips):
        p = TMP_DIR / f"{scene}__title_{i}_{c.label}.png"
        render_title_png(c.label, p)
        title_pngs.append(p)

    fc = build_filter(per_clip_w, hstack_w, cap_h)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(clips[0].path),
        "-i", str(clips[1].path),
        "-i", str(clips[2].path),
        "-i", str(cap_png),
        "-i", str(title_pngs[0]),
        "-i", str(title_pngs[1]),
        "-i", str(title_pngs[2]),
        "-filter_complex", fc,
        "-map", "[outv]",
        "-map", "[outa]",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"[{scene}] -> {out_path.name}")
    subprocess.run(cmd, check=True)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    scenes = find_scenes()
    print(f"Common scenes ({len(scenes)}): {scenes}")

    rng = random.Random(42)
    mapping: dict = {}
    positions = ["left", "middle", "right"]

    for scene in scenes:
        clips_by_label = {}
        ok = True
        for label, folder, pattern in SOURCES:
            p = folder / pattern.format(scene=scene)
            if not p.exists():
                print(f"  [skip] {label} missing {p}")
                ok = False
                break
            clips_by_label[label] = Clip(label=label, path=p)
        if not ok:
            continue

        ordered = [clips_by_label[l] for l, _, _ in SOURCES]
        rng.shuffle(ordered)

        caption = load_caption(scene)
        out_path = OUT_DIR / f"{scene}__blind.mp4"

        try:
            render_scene(scene, ordered, caption, out_path)
        except subprocess.CalledProcessError as e:
            print(f"  [fail] {scene}: rc={e.returncode}")
            continue

        mapping[scene] = {
            "left": ordered[0].label,
            "middle": ordered[1].label,
            "right": ordered[2].label,
            "caption": caption,
        }

    (OUT_DIR / "_mapping.json").write_text(json.dumps(mapping, indent=2))
    print(f"\nMapping (answer key) -> {OUT_DIR / '_mapping.json'}")


if __name__ == "__main__":
    main()
