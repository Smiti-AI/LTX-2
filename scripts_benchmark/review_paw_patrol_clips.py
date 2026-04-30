#!/usr/bin/env python3
"""Tiny local web app to review candidate Paw Patrol clips.

Shows one 5s clip at a time. Press Good or Bad (or 'g'/'b' keys).
- Good -> moves the clip into output/paw_patrol_review/data/
- Bad  -> moves the clip into output/paw_patrol_review/trash/
Decisions are persisted in decisions.json so the session is resumable.

Run:
    python3 scripts_benchmark/review_paw_patrol_clips.py
Then open http://localhost:8765
"""

from __future__ import annotations

import json
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path("/Users/efrattaig/projects/sm/LTX-2/output/paw_patrol_review")
CANDIDATES_DIR = ROOT / "candidates"
DATA_DIR = ROOT / "data"
TRASH_DIR = ROOT / "trash"
META_PATH = ROOT / "candidates_meta.json"
DECISIONS_PATH = ROOT / "decisions.json"
STATUS_PATH = ROOT / "STATUS.md"

PORT = 8770


def load_meta() -> list[dict]:
    return json.loads(META_PATH.read_text())


def load_decisions() -> dict[str, dict]:
    """Returns {filename: {verdict, note, ts, episode, clip_start, scene_duration}}.
    Older entries that were stored as plain strings are upgraded on the fly.
    """
    if not DECISIONS_PATH.exists():
        return {}
    raw = json.loads(DECISIONS_PATH.read_text())
    upgraded: dict[str, dict] = {}
    for k, v in raw.items():
        if isinstance(v, str):
            upgraded[k] = {"verdict": v, "note": "", "ts": None}
        else:
            upgraded[k] = v
    return upgraded


def save_decisions(d: dict[str, dict]) -> None:
    DECISIONS_PATH.write_text(json.dumps(d, indent=2))


def verdict_of(d: dict | str) -> str:
    return d["verdict"] if isinstance(d, dict) else d


def next_pending(meta: list[dict], decisions: dict[str, dict]) -> dict | None:
    for item in meta:
        if item["filename"] not in decisions:
            return item
    return None


def stats(meta: list[dict], decisions: dict[str, dict]) -> tuple[int, int, int]:
    good = sum(1 for v in decisions.values() if verdict_of(v) == "good")
    bad = sum(1 for v in decisions.values() if verdict_of(v) == "bad")
    remaining = sum(1 for m in meta if m["filename"] not in decisions)
    return good, bad, remaining


def write_status(meta: list[dict], decisions: dict[str, dict]) -> None:
    """Regenerate STATUS.md — overall, per-episode, plus good/bad lists with notes."""
    from collections import defaultdict
    from datetime import datetime, timezone

    good, bad, remaining = stats(meta, decisions)
    by_ep: dict[str, dict] = defaultdict(lambda: {"good": 0, "bad": 0, "pending": 0, "total": 0})
    meta_by_name = {m["filename"]: m for m in meta}
    for m in meta:
        ep = m["episode"]
        by_ep[ep]["total"] += 1
        d = decisions.get(m["filename"])
        if d is None:
            by_ep[ep]["pending"] += 1
        elif verdict_of(d) == "good":
            by_ep[ep]["good"] += 1
        else:
            by_ep[ep]["bad"] += 1

    lines: list[str] = []
    lines.append("# Paw Patrol clip review — status")
    lines.append("")
    lines.append(f"_Last updated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}_")
    lines.append("")
    lines.append(f"- Total candidates extracted: **{len(meta)}**")
    lines.append(f"- Good: **{good}**  ·  Bad: **{bad}**  ·  Pending: **{remaining}**")
    decided = good + bad
    if decided:
        lines.append(f"- Acceptance rate (good / decided): **{good/decided:.0%}**")
    lines.append("")
    lines.append("## Per-episode breakdown")
    lines.append("")
    lines.append("| Episode | Total | Good | Bad | Pending | Accept% |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for ep in sorted(by_ep):
        s = by_ep[ep]
        d = s["good"] + s["bad"]
        acc = f"{s['good']/d:.0%}" if d else "—"
        lines.append(f"| {ep} | {s['total']} | {s['good']} | {s['bad']} | {s['pending']} | {acc} |")
    lines.append("")

    def _row(filename: str, dec: dict) -> str:
        m = meta_by_name.get(filename, {})
        ep = m.get("episode", "?")
        cs = m.get("clip_start", "?")
        note = (dec.get("note") or "").replace("|", "\\|").replace("\n", " ")
        return f"| `{filename}` | {ep} | {cs} | {note} |"

    good_items = [(k, v) for k, v in decisions.items() if verdict_of(v) == "good"]
    bad_items = [(k, v) for k, v in decisions.items() if verdict_of(v) == "bad"]

    lines.append(f"## Good clips ({len(good_items)})")
    lines.append("")
    lines.append("| Clip | Episode | Start (s) | Note |")
    lines.append("|---|---|---:|---|")
    for k, v in sorted(good_items):
        lines.append(_row(k, v))
    lines.append("")
    lines.append(f"## Bad clips ({len(bad_items)})")
    lines.append("")
    lines.append("| Clip | Episode | Start (s) | Note |")
    lines.append("|---|---|---:|---|")
    for k, v in sorted(bad_items):
        lines.append(_row(k, v))
    lines.append("")
    lines.append("## How this stays consistent")
    lines.append("")
    lines.append("- `candidates_meta.json` is the canonical record of every clip ever extracted.")
    lines.append("- `decisions.json` stores per-clip `{verdict, note, ts}` keyed by filename.")
    lines.append("- `prepare_paw_patrol_clips.py` builds a `(episode, int(clip_start))` set from")
    lines.append("  `candidates_meta.json` and **skips** any window already extracted, so re-runs")
    lines.append("  on the same source files never produce duplicates — regardless of whether the")
    lines.append("  prior decision was good, bad, or pending.")
    lines.append("- This file (`STATUS.md`) is regenerated automatically after every decision.")
    lines.append("")

    STATUS_PATH.write_text("\n".join(lines))


INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Paw Patrol clip review</title>
<style>
  body { font-family: -apple-system, sans-serif; background:#111; color:#eee; margin:0; padding:24px; }
  .wrap { max-width: 960px; margin: 0 auto; }
  video { width:100%; max-height:70vh; background:#000; border-radius:8px; }
  .row { display:flex; gap:12px; margin-top:16px; align-items:center; }
  button { font-size:18px; padding:14px 24px; border:0; border-radius:8px; cursor:pointer; }
  .good { background:#1e7a3c; color:white; }
  .bad  { background:#a32424; color:white; }
  .meta { color:#aaa; font-size:14px; margin-top:8px; }
  .stats { color:#ccc; }
  .done { padding:40px; text-align:center; font-size:20px; }
  kbd { background:#222; border:1px solid #444; padding:1px 6px; border-radius:4px; font-size:12px; }
</style></head>
<body><div class="wrap" id="app">Loading...</div>
<script>
async function refresh() {
  const r = await fetch('/state'); const s = await r.json();
  const app = document.getElementById('app');
  if (!s.current) {
    app.innerHTML = `<div class="done">Done. Good: ${s.good}, Bad: ${s.bad}.<br><br>
      Good clips are in <code>output/paw_patrol_review/data/</code>.</div>`;
    return;
  }
  const c = s.current;
  app.innerHTML = `
    <div class="stats">Good: ${s.good} &nbsp; Bad: ${s.bad} &nbsp; Remaining: ${s.remaining}
      &nbsp; <a href="/gallery?which=data" style="color:#7af;">[view good]</a>
      <a href="/gallery?which=trash" style="color:#7af;">[view bad]</a></div>
    <h2>${c.filename}</h2>
    <video src="/clip/${encodeURIComponent(c.filename)}" controls autoplay></video>
    <div class="meta">${c.episode} &middot; scene ${c.scene_start}s–${c.scene_end}s &middot; clip starts at ${c.clip_start}s</div>
    <input id="note" type="text" placeholder="optional note — what's good/bad about it?"
      style="width:100%;margin-top:12px;padding:10px;font-size:14px;background:#222;color:#eee;border:1px solid #444;border-radius:6px;" />
    <div class="row">
      <button class="good" onclick="decide('good')">Good (g)</button>
      <button class="bad"  onclick="decide('bad')">Bad (b)</button>
      <span style="margin-left:auto;color:#888;">keys: <kbd>g</kbd> good, <kbd>b</kbd> bad — Enter while typing also submits Good</span>
    </div>`;
}
async function decide(verdict) {
  const fn = document.querySelector('h2').innerText;
  const note = (document.getElementById('note')?.value || '').trim();
  await fetch('/decide', {method:'POST', headers:{'content-type':'application/json'},
    body: JSON.stringify({filename: fn, verdict, note})});
  refresh();
}
window.addEventListener('keydown', e => {
  // Don't hijack g/b when typing in the note field.
  const inNote = document.activeElement && document.activeElement.id === 'note';
  if (inNote) {
    if (e.key === 'Enter') { e.preventDefault(); decide('good'); }
    return;
  }
  if (e.key === 'g') decide('good');
  else if (e.key === 'b') decide('bad');
});
refresh();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        if url.path == "/" or url.path == "/index.html":
            body = INDEX_HTML.encode()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if url.path == "/gallery":
            which = parse_qs(url.query).get("which", ["data"])[0]
            base = DATA_DIR if which == "data" else TRASH_DIR
            files = sorted(p.name for p in base.glob("*.mp4"))
            tiles = "".join(
                f'<div class="tile"><video src="/file/{which}/{n}" controls preload="metadata"></video>'
                f'<div class="cap">{n}</div>'
                f'<button onclick="undo(\'{n}\')">Move back to queue</button></div>'
                for n in files
            )
            html = f"""<!doctype html><html><head><meta charset=utf-8>
<title>Gallery — {which} ({len(files)})</title>
<style>
body{{font-family:-apple-system,sans-serif;background:#111;color:#eee;margin:0;padding:20px;}}
h1{{margin:0 0 16px;}} a{{color:#7af;}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px;}}
.tile{{background:#1a1a1a;border-radius:8px;padding:8px;}}
.tile video{{width:100%;border-radius:4px;background:#000;}}
.cap{{font-size:11px;color:#aaa;margin:6px 0;word-break:break-all;}}
button{{background:#444;color:#eee;border:0;padding:6px 10px;border-radius:4px;cursor:pointer;font-size:12px;}}
button:hover{{background:#666;}}
.nav{{margin-bottom:16px;}}
.nav a{{margin-right:14px;}}
</style></head><body>
<div class="nav"><a href="/">← Back to review</a>
<a href="/gallery?which=data">Good ({len(list(DATA_DIR.glob('*.mp4')))})</a>
<a href="/gallery?which=trash">Bad ({len(list(TRASH_DIR.glob('*.mp4')))})</a></div>
<h1>{which.title()} — {len(files)} clips</h1>
<div class="grid">{tiles}</div>
<script>
async function undo(name){{
  await fetch('/undo',{{method:'POST',headers:{{'content-type':'application/json'}},
    body:JSON.stringify({{filename:name,from:'{which}'}})}});
  location.reload();
}}
</script></body></html>"""
            body = html.encode()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if url.path.startswith("/file/"):
            parts = url.path.split("/", 3)
            if len(parts) == 4 and parts[2] in ("data", "trash"):
                base = DATA_DIR if parts[2] == "data" else TRASH_DIR
                path = base / parts[3]
                if path.exists():
                    data = path.read_bytes()
                    self.send_response(200)
                    self.send_header("content-type", "video/mp4")
                    self.send_header("content-length", str(len(data)))
                    self.send_header("accept-ranges", "bytes")
                    self.end_headers()
                    self.wfile.write(data)
                    return
            self.send_error(404)
            return
        if url.path == "/state":
            meta = load_meta()
            decisions = load_decisions()
            good, bad, remaining = stats(meta, decisions)
            current = next_pending(meta, decisions)
            self._json({"current": current, "good": good, "bad": bad, "remaining": remaining})
            return
        if url.path.startswith("/clip/"):
            name = url.path[len("/clip/"):]
            path = CANDIDATES_DIR / name
            if not path.exists():
                # Already moved (e.g. user pressed back) — try data/ then trash/.
                for alt in (DATA_DIR / name, TRASH_DIR / name):
                    if alt.exists():
                        path = alt
                        break
            if not path.exists():
                self.send_error(404)
                return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("content-type", "video/mp4")
            self.send_header("content-length", str(len(data)))
            self.send_header("accept-ranges", "bytes")
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404)

    def do_POST(self):  # noqa: N802
        url = urlparse(self.path)
        if url.path == "/undo":
            length = int(self.headers.get("content-length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            filename = payload.get("filename")
            src_kind = payload.get("from")
            if src_kind not in ("data", "trash") or not filename:
                self._json({"error": "bad request"}, status=400)
                return
            src_dir = DATA_DIR if src_kind == "data" else TRASH_DIR
            src = src_dir / filename
            if src.exists():
                shutil.move(str(src), str(CANDIDATES_DIR / filename))
            decisions = load_decisions()
            decisions.pop(filename, None)
            save_decisions(decisions)
            write_status(load_meta(), decisions)
            self._json({"ok": True})
            return
        if url.path != "/decide":
            self.send_error(404)
            return
        length = int(self.headers.get("content-length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        filename = payload.get("filename")
        verdict = payload.get("verdict")
        if verdict not in ("good", "bad") or not filename:
            self._json({"error": "bad request"}, status=400)
            return
        from datetime import datetime, timezone
        note = (payload.get("note") or "").strip()
        src = CANDIDATES_DIR / filename
        dst_dir = DATA_DIR if verdict == "good" else TRASH_DIR
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / filename
        if src.exists():
            shutil.move(str(src), str(dst))
        decisions = load_decisions()
        decisions[filename] = {
            "verdict": verdict,
            "note": note,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        save_decisions(decisions)
        write_status(load_meta(), decisions)
        self._json({"ok": True})


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    if not META_PATH.exists():
        raise SystemExit(f"No metadata at {META_PATH}. Run prepare_paw_patrol_clips.py first.")
    # Regenerate STATUS.md on startup so it reflects current state even after upgrades.
    write_status(load_meta(), load_decisions())
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Review UI at http://localhost:{PORT}")
    print(f"  candidates: {CANDIDATES_DIR}")
    print(f"  good ->     {DATA_DIR}")
    print(f"  bad  ->     {TRASH_DIR}")
    server.serve_forever()


if __name__ == "__main__":
    main()
