#!/usr/bin/env python3
"""
view_benchmark_gallery.py

Generates an HTML gallery from benchmark output videos and opens it in the browser.
Run locally after syncing results from the server.

Usage
-----
    # 1. Sync results from server
    rsync -av --include='*.mp4' --include='*/' --exclude='*' \
        efrattaig@35.238.2.51:/home/efrattaig/LTX-2/output/benchmarks/ \
        output/benchmarks/

    # 2. Generate and open gallery
    python view_benchmark_gallery.py
"""

from __future__ import annotations

import base64
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BENCHMARKS_DIR = SCRIPT_DIR / "output" / "benchmarks"
OUTPUT_HTML = BENCHMARKS_DIR / "index.html"

SCENE_ORDER = [
    "bey", "crsms", "holoween", "party", "snow",
    "of_silly_goose", "of_eagle", "of_lifeguard", "of_lift", "of_everest",
]
SCENE_LABELS = {
    "bey":           "Lookout (sunny)",
    "crsms":         "Christmas",
    "holoween":      "Halloween",
    "party":         "Party",
    "snow":          "Snowy lookout",
    "of_silly_goose": "🔬 Silly goose",
    "of_eagle":       "🔬 Eagle warning",
    "of_lifeguard":   "🔬 Lifeguard",
    "of_lift":        "🔬 Lift/harness",
    "of_everest":     "🔬 Everest visit",
}


def collect_videos(base: Path) -> dict:
    """
    Returns:
        {exp_name: {step: {scene: Path}}}
    """
    data: dict = {}
    for mp4 in sorted(base.rglob("*.mp4")):
        # Expected layout: benchmarks/{exp_name}/step_{NNNNN}/{scene}.mp4
        try:
            rel = mp4.relative_to(base)
            parts = rel.parts
            if len(parts) != 3:
                continue
            exp_name, step_dir, scene_file = parts
            if not step_dir.startswith("step_"):
                continue
            step = int(step_dir.split("_")[1])
            scene = scene_file.replace(".mp4", "")
        except (ValueError, IndexError):
            continue

        data.setdefault(exp_name, {}).setdefault(step, {})[scene] = mp4

    return data


def video_src(path: Path) -> str:
    """Return a data URI so the HTML file is self-contained (no server needed)."""
    data = base64.b64encode(path.read_bytes()).decode()
    return f"data:video/mp4;base64,{data}"


def build_html(data: dict) -> str:
    if not data:
        return "<html><body><h1>No benchmark videos found yet.</h1></body></html>"

    all_scenes = SCENE_ORDER + sorted(
        s for s in {s for exp in data.values() for step in exp.values() for s in step}
        if s not in SCENE_ORDER
    )

    rows_html = []
    for exp_name in sorted(data):
        for step in sorted(data[exp_name]):
            scenes = data[exp_name][step]
            cells = []
            for scene in all_scenes:
                label = SCENE_LABELS.get(scene, scene)
                if scene in scenes:
                    src = video_src(scenes[scene])
                    cell = f"""
                        <td>
                          <div class="label">{label}</div>
                          <video controls loop muted width="240" src="{src}"></video>
                        </td>"""
                else:
                    cell = f'<td><div class="label">{label}</div><div class="missing">—</div></td>'
                cells.append(cell)

            row = f"""
            <tr>
              <td class="meta">
                <div class="exp">{exp_name.replace("skye_", "")}</div>
                <div class="step">step {step:,}</div>
              </td>
              {"".join(cells)}
            </tr>"""
            rows_html.append(row)

    scene_headers = "".join(
        f'<th>{SCENE_LABELS.get(s, s)}</th>' for s in all_scenes
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Skye LoRA Benchmark</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #111; color: #eee; margin: 20px; }}
  h1 {{ color: #fff; margin-bottom: 4px; }}
  p.sub {{ color: #888; margin-top: 0; margin-bottom: 20px; font-size: 13px; }}
  table {{ border-collapse: collapse; }}
  th, td {{ padding: 8px 10px; text-align: center; vertical-align: top; }}
  th {{ background: #222; color: #aaa; font-size: 12px; font-weight: 600;
        text-transform: uppercase; letter-spacing: .05em; border-bottom: 1px solid #333; }}
  tr:nth-child(even) {{ background: #181818; }}
  tr:hover {{ background: #1e1e1e; }}
  td.meta {{ text-align: left; white-space: nowrap; border-right: 1px solid #333;
             min-width: 160px; }}
  .exp  {{ font-size: 13px; font-weight: 700; color: #7cf; }}
  .step {{ font-size: 12px; color: #888; margin-top: 2px; }}
  .label {{ font-size: 11px; color: #888; margin-bottom: 4px; }}
  .missing {{ color: #444; font-size: 20px; padding: 40px 20px; }}
  video {{ display: block; border-radius: 6px; border: 1px solid #333; }}
  video:hover {{ border-color: #7cf; }}
</style>
</head>
<body>
<h1>Skye LoRA Benchmark</h1>
<p class="sub">Each row = one checkpoint &nbsp;·&nbsp; Each column = one benchmark scene &nbsp;·&nbsp;
All videos use fixed seed=42, 5 s, 768×1024</p>
<table>
  <thead>
    <tr>
      <th>Experiment / Step</th>
      {scene_headers}
    </tr>
  </thead>
  <tbody>
    {"".join(rows_html)}
  </tbody>
</table>
</body>
</html>"""


def main() -> int:
    if not BENCHMARKS_DIR.exists():
        print(f"Benchmarks dir not found: {BENCHMARKS_DIR}")
        print("Run the rsync command first (see script docstring).")
        return 1

    data = collect_videos(BENCHMARKS_DIR)
    total = sum(len(scenes) for exp in data.values() for scenes in exp.values())

    if not data:
        print("No benchmark videos found yet — training hasn't produced checkpoints.")
        return 0

    print(f"Found {total} video(s) across {len(data)} experiment(s).")
    print("Building gallery (embedding videos into HTML — may take a moment)…")

    html = build_html(data)
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    print(f"Gallery written: {OUTPUT_HTML}")

    if sys.platform == "darwin":
        subprocess.run(["open", str(OUTPUT_HTML)])
    elif sys.platform.startswith("linux"):
        subprocess.run(["xdg-open", str(OUTPUT_HTML)])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
