#!/usr/bin/env python3
"""
view_compare2.py

Clean 2-checkpoint comparison: one scene per row, two columns, all synced.

Sync model
----------
  Video[0] (top-left) is the MASTER.
  All others are FOLLOWERS — synced to master via timeupdate + drift correction.
  Clicking any video, or the transport buttons, controls the master.
  A shared scrub bar + time display at the top.

Audio
-----
  Videos are served with audio. Only the master plays audio (others are muted)
  so you hear one clean audio track, not a chorus.

Usage
-----
    python view_compare2.py
    python view_compare2.py --port 8768
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import threading
import webbrowser
from pathlib import Path

SCRIPT_DIR  = Path(__file__).resolve().parent
RESULTS_DIR = SCRIPT_DIR / "lora_results_FIX" / "benchmarks"

SCENE_ORDER = ["bey", "crsms", "holoween", "party", "snow",
               "of_silly_goose", "of_eagle", "of_lifeguard", "of_lift", "of_everest"]
SCENE_LABELS = {
    "bey":            "Lookout (sunny)",
    "crsms":          "Christmas",
    "holoween":       "Halloween",
    "party":          "Party",
    "snow":           "Snowy lookout",
    "of_silly_goose": "🔬 Silly goose",
    "of_eagle":       "🔬 Eagle warning",
    "of_lifeguard":   "🔬 Lifeguard",
    "of_lift":        "🔬 Lift / harness",
    "of_everest":     "🔬 Everest visit",
}
EXP_LABELS = {
    "skye_exp1_baseline": "EXP-1  Baseline  (rank 16, 1k steps)",
    "skye_exp2_standard": "EXP-2  T2V  (rank 32, 5k steps)",
    "skye_exp2_10k":      "EXP-2-10K  T2V  (rank 32, 10k steps)",
    "skye_exp4_i2v":      "EXP-4  I2V  (rank 32, 2.5k steps)",
    "skye_exp4_10k":      "EXP-4-10K  I2V  (rank 32, 10k steps)",
}
# EXP-3 (highcap / highres) was never run — no data exists for it.


def discover() -> dict:
    """{ exp: { step_int: { scene: rel_path_to_lora_mp4 } } }"""
    data: dict[str, dict[int, dict[str, str]]] = {}
    for exp_dir in sorted(RESULTS_DIR.iterdir()):
        if not exp_dir.is_dir() or exp_dir.name.startswith("_"):
            continue
        exp = exp_dir.name
        data[exp] = {}
        for step_dir in sorted(exp_dir.iterdir()):
            if not step_dir.is_dir():
                continue
            step = int(step_dir.name.replace("step_", ""))
            scenes: dict[str, str] = {}
            for f in step_dir.glob("*_lora.mp4"):
                scene = f.stem.replace("_lora", "")
                scenes[scene] = str(f.relative_to(SCRIPT_DIR))
            if scenes:
                data[exp][step] = scenes
    return data


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Compare two checkpoints</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0a0a0a; color: #ddd; font-family: system-ui, sans-serif; }

/* ── header ── */
.header { padding: 16px 20px 12px; border-bottom: 1px solid #1a1a1a;
          display: flex; align-items: center; gap: 20px; flex-wrap: wrap; }
h1 { font-size: 15px; color: #fff; white-space: nowrap; }

.picker { display: flex; align-items: center; gap: 10px; }
.picker-block { display: flex; flex-direction: column; gap: 3px; }
.picker-block label { font-size: 10px; color: #555; text-transform: uppercase; letter-spacing: .05em; }
.picker-block select { background: #161616; color: #ccc; border: 1px solid #282828;
  border-radius: 7px; padding: 6px 10px; font-size: 12px; min-width: 200px; cursor: pointer; }
.vs { font-size: 18px; color: #333; font-weight: 700; padding-top: 16px; }

/* ── transport bar ── */
.transport { background: #111; border-bottom: 1px solid #1a1a1a;
             padding: 8px 20px; display: flex; align-items: center; gap: 12px; }
.btn { cursor: pointer; padding: 6px 16px; border-radius: 7px; border: none;
       font-size: 12px; font-weight: 600; letter-spacing: .02em; }
.btn-play    { background: #2563eb; color: #fff; }
.btn-play:hover { background: #1d4ed8; }
.btn-pause   { background: #1e1e1e; color: #aaa; border: 1px solid #2a2a2a; }
.btn-restart { background: #1e1e1e; color: #aaa; border: 1px solid #2a2a2a; }
.btn-pause:hover, .btn-restart:hover { background: #252525; }

.progress-wrap { flex: 1; display: flex; align-items: center; gap: 8px; }
#progress { flex: 1; accent-color: #2563eb; cursor: pointer; height: 4px; }
#time-disp { font-size: 11px; color: #555; min-width: 40px; text-align: right; font-variant-numeric: tabular-nums; }
#status { font-size: 11px; color: #c0392b; min-width: 180px; }

/* ── column headers ── */
.col-headers { display: grid; grid-template-columns: 120px 1fr 1fr; }
.chdr { padding: 9px 12px 7px; font-size: 12px; font-weight: 600; text-align: center;
        background: #0f0f0f; border-bottom: 2px solid #1a1a1a; }
.chdr.left  { color: #60a5fa; border-left: 3px solid #2563eb; }
.chdr.right { color: #fb923c; border-left: 3px solid #ea580c; }
.chdr.scene-hdr { color: #333; font-size: 10px; text-transform: uppercase; letter-spacing: .07em;
                  text-align: left; }

/* ── rows ── */
.scene-row { display: grid; grid-template-columns: 120px 1fr 1fr;
             border-bottom: 1px solid #111; }
.scene-row:hover { background: #0d0d0d; }
.scene-lbl { display: flex; align-items: center; padding: 0 10px;
             font-size: 12px; color: #777; font-weight: 500; }

.vcell { padding: 6px 8px; }
video { width: 100%; border-radius: 7px; background: #000; display: block; cursor: pointer; }
video.master { border-top: 3px solid #2563eb55; }
.vcell.right video { border-top: 3px solid #ea580c55; }

.section-sep { grid-column: 1/-1; background: #0f0f0f; color: #2a2a2a;
  font-size: 10px; text-transform: uppercase; letter-spacing: .07em; padding: 5px 14px; }
</style>
</head>
<body>

<div class="header">
  <h1>Compare two checkpoints</h1>
  <div class="picker">
    <div class="picker-block">
      <label>Left (blue)</label>
      <select id="sel-left"></select>
    </div>
    <span class="vs">vs</span>
    <div class="picker-block">
      <label>Right (orange)</label>
      <select id="sel-right"></select>
    </div>
  </div>
</div>

<div class="transport">
  <button class="btn btn-restart" onclick="restart()">↺ Restart</button>
  <button class="btn btn-pause"   onclick="pause()">⏸ Pause</button>
  <button class="btn btn-play"    onclick="play()">▶ Play all</button>
  <div class="progress-wrap">
    <input type="range" id="progress" min="0" max="1000" value="0">
    <span id="time-disp">0.0 s</span>
  </div>
  <span id="status"></span>
</div>

<div class="col-headers">
  <div class="chdr scene-hdr">Scene</div>
  <div class="chdr left"  id="hdr-left">—</div>
  <div class="chdr right" id="hdr-right">—</div>
</div>

<div id="rows"></div>

<script>
const DATA        = __DATA__;
const EXP_LABELS  = __EXP_LABELS__;
const SCENE_ORDER = __SCENE_ORDER__;
const SCENE_LABELS= __SCENE_LABELS__;
const MAIN_SCENES = ["bey","crsms","holoween","party","snow"];

// ── Options list ─────────────────────────────────────────────────────────────
const OPTIONS = [];
Object.entries(DATA).forEach(([exp, steps]) => {
    const label = EXP_LABELS[exp] || exp;
    Object.keys(steps).map(Number).sort((a,b)=>a-b).forEach(step => {
        OPTIONS.push({ exp, step, label: `${label}  —  step ${step.toLocaleString()}` });
    });
});

function populateSelect(id, defExp, defStep) {
    const sel = document.getElementById(id);
    OPTIONS.forEach((o, i) => {
        const opt = document.createElement('option');
        opt.value = i;
        opt.textContent = o.label;
        if (o.exp === defExp && o.step === defStep) opt.selected = true;
        sel.appendChild(opt);
    });
    sel.addEventListener('change', render);
}
populateSelect('sel-left',  'skye_exp2_10k', 5000);
populateSelect('sel-right', 'skye_exp2_10k', 10000);

// ── Master-follower sync ──────────────────────────────────────────────────────
let master = null;
const status = document.getElementById('status');
const progress = document.getElementById('progress');
const timeDisp = document.getElementById('time-disp');

function followers() {
    const all = [...document.querySelectorAll('video')];
    return all.filter(v => v !== master);
}
function allVids() { return [...document.querySelectorAll('video')]; }

function attachSync() {
    const vs = allVids();
    if (!vs.length) { status.textContent = 'No videos found'; return; }

    master = vs[0];
    master.classList.add('master');
    status.textContent = '';

    // Master drives followers
    master.addEventListener('timeupdate', () => {
        const t = master.currentTime;
        // Update scrub bar
        if (master.duration) {
            progress.value = Math.round((t / master.duration) * 1000);
            timeDisp.textContent = t.toFixed(1) + ' s';
        }
        // Sync followers (correct drift > 0.15s)
        followers().forEach(v => {
            if (Math.abs(v.currentTime - t) > 0.15) v.currentTime = t;
        });
    });

    master.addEventListener('play',  () => followers().forEach(v => v.play().catch(()=>{})));
    master.addEventListener('pause', () => followers().forEach(v => v.pause()));
    master.addEventListener('ended', () => followers().forEach(v => v.pause()));

    master.addEventListener('error', e => {
        status.textContent = 'Error loading master video: ' + (master.error?.message || '?');
    });

    // Scrub bar seeks all videos
    progress.addEventListener('input', () => {
        if (!master.duration) return;
        const t = (progress.value / 1000) * master.duration;
        allVids().forEach(v => { v.currentTime = t; });
        timeDisp.textContent = t.toFixed(1) + ' s';
    });

    // Click any video = toggle play/pause via master
    allVids().forEach(v => {
        v.addEventListener('click', () => {
            if (master.paused) master.play().catch(err => { status.textContent = '▶ failed: ' + err.message; });
            else master.pause();
        });
    });
}

function play() {
    if (!master) { status.textContent = 'No videos loaded yet'; return; }
    master.currentTime = 0;
    const p = master.play();
    if (p) p.catch(err => { status.textContent = '▶ failed: ' + err.message; });
}
function pause()   { if (master) master.pause(); }
function restart() { play(); }

// ── Render ───────────────────────────────────────────────────────────────────
function render() {
    const lo = OPTIONS[document.getElementById('sel-left').value];
    const ro = OPTIONS[document.getElementById('sel-right').value];

    document.getElementById('hdr-left').textContent  = lo.label;
    document.getElementById('hdr-right').textContent = ro.label;

    const lScenes = DATA[lo.exp]?.[String(lo.step)] || {};
    const rScenes = DATA[ro.exp]?.[String(ro.step)] || {};

    let html = '';
    let overfit = false;
    SCENE_ORDER.forEach(scene => {
        if (!MAIN_SCENES.includes(scene) && !overfit) {
            overfit = true;
            html += '<div class="scene-row"><div class="section-sep">Overfit / training-data scenes</div></div>';
        }
        const ls = lScenes[scene], rs = rScenes[scene];
        html += `<div class="scene-row">`;
        html += `<div class="scene-lbl">${SCENE_LABELS[scene] || scene}</div>`;
        html += `<div class="vcell left">`  + (ls ? `<video src="/${ls}" preload="auto" playsinline></video>` : '<div style="color:#222;padding:20px;text-align:center">—</div>') + `</div>`;
        html += `<div class="vcell right">` + (rs ? `<video src="/${rs}" preload="auto" muted playsinline></video>` : '<div style="color:#222;padding:20px;text-align:center">—</div>') + `</div>`;
        html += `</div>`;
    });

    document.getElementById('rows').innerHTML = html;

    // Reset scrub bar
    progress.value = 0;
    timeDisp.textContent = '0.0 s';
    master = null;

    attachSync();
}

render();
</script>
</body>
</html>
"""


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, page_html: str, **kwargs):
        self._html = page_html
        super().__init__(*args, directory=str(SCRIPT_DIR), **kwargs)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = self._html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()

    def log_message(self, *_): pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8768)
    args = parser.parse_args()

    data = discover()
    data_js = {
        exp: {str(step): scenes for step, scenes in steps.items()}
        for exp, steps in data.items()
    }

    page_html = (
        HTML
        .replace("__DATA__",        json.dumps(data_js))
        .replace("__EXP_LABELS__",  json.dumps(EXP_LABELS))
        .replace("__SCENE_ORDER__", json.dumps(SCENE_ORDER))
        .replace("__SCENE_LABELS__",json.dumps(SCENE_LABELS))
    )

    handler = functools.partial(Handler, page_html=page_html)
    server  = http.server.HTTPServer(("127.0.0.1", args.port), handler)
    print(f"Gallery: http://127.0.0.1:{args.port}")
    print("Defaults: EXP-2-10K step 5,000 (left, with audio) vs step 10,000 (right, muted)")
    print("Note: EXP-3 was never run — no data exists for it.")
    print("Press Ctrl+C to stop.\n")
    threading.Timer(0.4, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
