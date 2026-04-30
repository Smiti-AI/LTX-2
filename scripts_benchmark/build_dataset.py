#!/usr/bin/env python3
"""Build a Paw Patrol training dataset on the laptop.

Reads curated 5s clips (typically the output of the manual review UI in
`scripts_benchmark/review_paw_patrol_clips.py`), copies them as
`raw_videos/0001.mp4`–`NNNN.mp4`, captions each via Gemini 2.5 Pro on
Vertex AI, and writes `metadata.json` mirroring the skye_golden_v2 schema.

Output layout::

    <out-root>/
        raw_videos/0001.mp4 ... NNNN.mp4
        metadata.json

Captions use raw character names (e.g. "Skye", "Chase"). Brand-token
substitution (`Skye → SKYE_PP`) happens later at gallery-render and
CSV-emit time per `scripts/dataset_pipeline/brand_tokens.yaml`. Do NOT
pre-substitute here.

Usage::

    uv run --no-project --isolated --with google-genai \\
      python scripts_benchmark/build_dataset.py \\
        --name mixed_cast_v1 \\
        --workers 6

Next step (after captions):

    gcloud storage cp -r output/<name>/raw_videos output/<name>/metadata.json \\
        gs://video_gen_dataset/TinyStories/training_data/<name>/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Defaults for the standard laptop layout.
DEFAULT_REVIEW_ROOT = Path("/Users/efrattaig/projects/sm/LTX-2/output/paw_patrol_review")
DEFAULT_OUT_ROOT_PARENT = Path("/Users/efrattaig/projects/sm/LTX-2/output")

PROJECT = "shapeshifter-459611"
LOCATION = "us-central1"
MODEL = "gemini-2.5-pro"

# Caption style mirrors skye_golden_v2/metadata.json: raw character names
# (no SKYE_PP token — substitution is done later via brand_tokens.yaml at
# gallery-render and CSV-emit time), detailed cinematic prose, includes
# camera framing, character description, motion arc, and dialogue.
PROMPT = """You are looking at a 5-second animated clip from PAW Patrol.

Identify characters using these EXACT names (never "the pup", "the team", "they"):
- Chase: German Shepherd puppy in a BLUE police uniform with a blue cap and star badge
- Skye: cockapoo with golden-tan curly fur in a PINK uniform with a pink helmet and goggles
- Marshall: Dalmatian (white with black spots) in a RED firefighter outfit and helmet
- Rocky: gray-and-white mixed-breed pup in a GREEN recycling outfit and cap
- Zuma: chocolate Labrador puppy in an ORANGE diving outfit and helmet
- Rubble: English Bulldog puppy in a YELLOW construction outfit and hard hat
- Everest: Siberian Husky puppy in a TURQUOISE winter outfit
- Tracker: Chihuahua in a YELLOW jungle ranger outfit
- Ryder: 10-year-old HUMAN boy in a red baseball cap and red-and-blue outfit, stands UPRIGHT on two legs
- Lookout tower: tall white tower with a yellow paw-print symbol
- Adventure Bay: small seaside town with colorful buildings

Write ONE detailed cinematic caption describing this clip. Style guide:
- Open with the camera framing (e.g. "A medium close-up shot frames…", "A wide low-angle shot of…", "A fixed shot captures…", "A slow pan across…").
- Name every visible character explicitly. Briefly describe their appearance the first time they appear in the caption (color, outfit, ears/posture).
- Describe motion as a temporal arc: what happens at the start, then mid-clip, then end. Use concrete verbs (running, leaping, sliding, gesturing, tilting head, opening eyes, looking off-screen, etc.).
- Mention the setting (Adventure Bay street, the Lookout interior, a beach, a forest path, snow, sky, etc.).
- If dialogue is heard, transcribe it verbatim in double quotes attributed by name: `Skye says, "Ready for action!"`. If multiple characters speak, include each. If only barks, sound effects, or music — describe the audio briefly instead.
- 2–4 sentences, 60–110 words. Factual tone — no "amazing", "exciting", "adorable", "happily".
- Output ONLY the caption. No bullet points, no preamble, no commentary."""


def md5(path: Path) -> str:
    h = hashlib.md5()  # noqa: S324 - md5 is fine for content-addressed identity here
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ffprobe(path: Path) -> dict:
    out = subprocess.check_output([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,duration:format=duration",
        "-of", "json", str(path),
    ])
    j = json.loads(out)
    s = j["streams"][0]
    fmt = j.get("format", {})
    num, den = s["r_frame_rate"].split("/")
    fps = float(num) / float(den) if float(den) else 0.0
    duration = float(s.get("duration") or fmt.get("duration") or 0.0)
    return {"width": int(s["width"]), "height": int(s["height"]), "fps": round(fps, 3),
            "duration_s": round(duration, 3)}


def caption_one(client, video_bytes: bytes, max_retries: int = 4) -> str | None:
    from google.genai import types  # noqa: PLC0415
    for attempt in range(max_retries):
        try:
            resp = client.models.generate_content(
                model=MODEL,
                contents=[
                    types.Part.from_bytes(data=video_bytes, mime_type="video/mp4"),
                    PROMPT,
                ],
            )
            text = (resp.text or "").strip()
            return text or None
        except Exception as e:  # noqa: BLE001
            wait = 2 ** attempt
            print(f"  retry {attempt + 1}/{max_retries} after {type(e).__name__}: {str(e)[:140]}",
                  flush=True)
            time.sleep(wait)
    return None


def stage_one(in_path: Path, idx: int, out_raw: Path,
              decisions: dict[str, dict],
              cand_meta_by_name: dict[str, dict]) -> dict:
    """Copy clip to raw_videos/NNNN.mp4 and assemble its metadata stub."""
    new_name = f"{idx:04d}.mp4"
    dst = out_raw / new_name
    if not dst.exists():
        shutil.copy2(in_path, dst)
    info = ffprobe(dst)
    cm = cand_meta_by_name.get(in_path.name, {})
    dec = decisions.get(in_path.name, {})
    note = ""
    if isinstance(dec, dict):
        note = (dec.get("note") or "").strip()
    return {
        "index": idx,
        "source_clip_filename": in_path.name,
        "source_url": "",
        "source_episode": cm.get("episode", ""),
        "source_clip_start_s": cm.get("clip_start"),
        "source_scene_start_s": cm.get("scene_start"),
        "source_scene_end_s": cm.get("scene_end"),
        "video": f"raw_videos/{new_name}",
        "video_md5": md5(dst),
        "prompt": "",
        "duration_s": info["duration_s"],
        "width": info["width"],
        "height": info["height"],
        "fps": info["fps"],
        "download_ok": True,
        "note": note,
    }


def make_metadata(name: str, character: str, clips: list[dict],
                  source_label: str, review_root: Path) -> dict:
    return {
        "dataset_id": name,
        "character": character,
        "version": 1,
        "poc": False,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": {
            "manifest_label": source_label,
            "review_root": str(review_root),
        },
        "captioning": {
            "version": "v1",
            "model": f"{MODEL} via Vertex AI (project={PROJECT})",
            "brand_token_format": "CHASE_PP / SKYE_PP",
            "substitution_at_build_time": True,
        },
        "stats": {
            "active_clips": sum(1 for c in clips if c.get("prompt")),
            "missing_clips": sum(1 for c in clips if not c.get("prompt")),
        },
        "clips": clips,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a Paw Patrol training dataset (rename + caption + metadata).",
    )
    parser.add_argument("--name", required=True,
                        help="Dataset id, e.g. 'mixed_cast_v1'. Used as the output dirname "
                             "and as `dataset_id` in metadata.json.")
    parser.add_argument("--character", default="mixed",
                        help="Value for `character` in metadata.json (e.g. 'mixed', 'skye'). "
                             "Default: 'mixed'.")
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="Folder of curated source mp4s. "
                             "Default: <review-root>/data")
    parser.add_argument("--review-root", type=Path, default=DEFAULT_REVIEW_ROOT,
                        help="Where decisions.json + candidates_meta.json live, for note "
                             "carry-over and source-episode lookup. "
                             f"Default: {DEFAULT_REVIEW_ROOT}")
    parser.add_argument("--out-root", type=Path, default=None,
                        help="Local staging dir for the new dataset. "
                             "Default: output/<name>/")
    parser.add_argument("--source-label", default=None,
                        help="Free-text source description in metadata.json. "
                             "Default: derived from review-root.")
    parser.add_argument("--workers", type=int, default=5,
                        help="Parallel Gemini calls (default 5).")
    parser.add_argument("--limit", type=int, default=0,
                        help="Only process first N clips (for testing). 0 = all.")
    parser.add_argument("--skip-captions", action="store_true",
                        help="Build raw_videos/ and metadata.json with empty prompts only.")
    args = parser.parse_args()

    review_root: Path = args.review_root
    data_dir: Path = args.data_dir or (review_root / "data")
    out_root: Path = args.out_root or (DEFAULT_OUT_ROOT_PARENT / args.name)
    out_raw = out_root / "raw_videos"
    out_metadata = out_root / "metadata.json"
    decisions_path = review_root / "decisions.json"
    candidates_meta_path = review_root / "candidates_meta.json"
    source_label = args.source_label or f"manual review at {review_root}"

    if not data_dir.exists():
        print(f"ERROR: data dir not found: {data_dir}", file=sys.stderr)
        return 2

    out_raw.mkdir(parents=True, exist_ok=True)

    decisions = json.loads(decisions_path.read_text()) if decisions_path.exists() else {}
    cand_meta = json.loads(candidates_meta_path.read_text()) if candidates_meta_path.exists() else []
    cand_meta_by_name = {c["filename"]: c for c in cand_meta}

    # Stage 1 — rename + metadata stubs
    sources = sorted(data_dir.glob("*.mp4"))
    if args.limit:
        sources = sources[:args.limit]
    print(f"Building dataset '{args.name}' (character='{args.character}')", flush=True)
    print(f"  source: {data_dir}  ({len(sources)} clips)", flush=True)
    print(f"  output: {out_root}", flush=True)
    print(f"Staging {len(sources)} clips → {out_raw}", flush=True)
    clips: list[dict] = []
    for i, src in enumerate(sources, start=1):
        clips.append(stage_one(src, i, out_raw, decisions, cand_meta_by_name))
        if i % 10 == 0 or i == len(sources):
            print(f"  staged {i}/{len(sources)}", flush=True)

    if args.skip_captions:
        meta = make_metadata(args.name, args.character, clips, source_label, review_root)
        out_metadata.write_text(json.dumps(meta, indent=2))
        print(f"\n--skip-captions: wrote stub metadata to {out_metadata}")
        return 0

    # Stage 2 — caption via Vertex Gemini
    from google import genai  # noqa: PLC0415
    client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)

    print(f"\nCaptioning {len(clips)} clips with {MODEL} via Vertex AI "
          f"({args.workers} workers)…\n", flush=True)
    t0 = time.time()
    failures: list[int] = []

    def _do(c: dict) -> tuple[int, str | None]:
        path = out_raw / Path(c["video"]).name
        text = caption_one(client, path.read_bytes())
        return c["index"], text

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_do, c): c for c in clips}
        done = 0
        for fut in as_completed(futures):
            idx, text = fut.result()
            done += 1
            c = next(x for x in clips if x["index"] == idx)
            if text:
                c["prompt"] = text
                preview = text[:120].replace("\n", " ")
                print(f"[{done}/{len(clips)}] {c['video']}: {preview}{'...' if len(text)>120 else ''}",
                      flush=True)
            else:
                failures.append(idx)
                print(f"[{done}/{len(clips)}] {c['video']}: FAILED after retries", flush=True)

            # Write incrementally so a crash doesn't lose work.
            meta = make_metadata(args.name, args.character, clips, source_label, review_root)
            out_metadata.write_text(json.dumps(meta, indent=2))

    dt = time.time() - t0
    print(f"\nDone in {dt/60:.1f} min. Failures: {len(failures)}", flush=True)
    if failures:
        print("Failed indices:", failures, flush=True)
        print("Re-run with the same --name to refill empty prompts (the script overwrites "
              "metadata.json incrementally; clips with non-empty prompts are skipped only if "
              "you separately filter — for now just retry the whole pass).", flush=True)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
