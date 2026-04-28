# Training Data Pipeline — Rebuild Proposal

> **Status:** proposal. Nothing executes until you greenlight.
> **Principle:** clean rebuild over salvage. Anything we can't trivially verify, we regenerate from raw + manifests.

---

## 1. Inputs (the only sources of truth from now on)

| What | Where | Frozen? |
|---|---|---|
| Golden Set manifests | `gs://video_gen_dataset/dataset/labelbox/{chase,skye}_golden.json` (+ companion `.parquet` for tabular form) | yes — single source of clip-list + captions |
| Raw broadcast episodes | `gs://video_gen_dataset/raw/paw_patrol/season_*/compressed_videos/PawPatrol_SXX_EYY_*.mp4` | yes — never edited |

Everything outside these two paths is treated as *output of an old pipeline* and ignored.

---

## 2. Canonical directory layout (per dataset)

```
gs://video_gen_dataset/training_data/{dataset_id}/
    metadata.json                  ← master file, the only thing trainer + manage.py read
    videos/
        0001.mp4
        0002.mp4
        ...
        0NNN.mp4                   ← clip filenames are zero-padded indices, never renumbered
    prompts/
        0001.txt                   ← one caption per clip, plain UTF-8
        0002.txt
        ...
        all.csv                    ← rebuilt aggregate (index, prompt) for bulk view/edit
    latents/
        0001.pt                    ← preprocessed for the trainer
        0002.pt
        ...
    gallery/
        gallery.mp4                ← THE QC artifact (see §4)
        per_clip/                  ← intermediate annotated clips (regeneration source for gallery)
            0001.mp4
            ...
    audit/
        build.log                  ← what ran, when, with which versions
        deletions.log              ← every `manage.py delete` invocation appended here
```

**`{dataset_id}` convention:** `{character_or_set}_golden_v{N}_{YYYYMMDD}`
- `chase_golden_v1_20260428`
- `skye_golden_v1_20260428`
- `full_cast_v1_20260501` (later)

Bumps `vN` only on a *full rebuild* (manifest changed, captioning re-done, encoding params changed). Deletions within a version stay in the same `vN` and gaps are kept (no renumbering).

---

## 3. `metadata.json` schema

The single load-bearing file. Trainer reads this; `manage.py` writes it.

```json
{
  "dataset_id": "chase_golden_v1_20260428",
  "character": "chase",
  "version": 1,
  "created_at": "2026-04-28T18:00:00Z",
  "git_commit": "abcdef12...",

  "source": {
    "manifest": "gs://video_gen_dataset/dataset/labelbox/chase_golden.json",
    "manifest_md5": "...",
    "raw_footage_root": "gs://video_gen_dataset/raw/paw_patrol/"
  },

  "encoding": {
    "resolution": [960, 544],
    "fps": 24,
    "frames_per_clip": 49,
    "codec": "libx264",
    "container": "mp4"
  },

  "captioning": {
    "source": "labelbox_manifest_field:caption",
    "version": "v1",
    "brand_token_format": "CHASE_PP"
  },

  "stats": {
    "active_clips": 137,
    "deleted_clips": 13,
    "total_duration_s": 274.0
  },

  "clips": [
    {
      "index": 1,
      "status": "active",                   // "active" | "deleted"
      "source_episode": "PawPatrol_S01_E06_A",
      "source_time_range_s": [120.5, 122.5],
      "video": "videos/0001.mp4",
      "video_md5": "...",
      "prompt": "videos/0001.txt content here, mirrored for grep convenience",
      "prompt_path": "prompts/0001.txt",
      "latent_path": "latents/0001.pt",
      "duration_s": 2.0,
      "width": 960,
      "height": 544,
      "fps": 24
    },
    {
      "index": 42,
      "status": "deleted",
      "deletion": {
        "reason": "low quality, character off-model",
        "deleted_at": "2026-04-30T12:34:00Z"
      }
    }
  ],

  "gallery": {
    "path": "gallery/gallery.mp4",
    "interclip_black_s": 0.5,
    "rendered_at": "2026-04-28T18:30:00Z",
    "rendered_index_count": 137
  }
}
```

The `prompt` field on each clip is duplicated (sidecar file *and* in metadata) so a single `cat metadata.json | jq` shows everything. Duplicate's source of truth = the sidecar file. `manage.py` re-syncs into metadata on every change.

---

## 4. Gallery video — the QC artifact

### Visual layout

```
┌────────────────────────────────────┐
│ #0001                              │  ← top-left, large, white-on-black box
│                                    │
│      [original clip pixels]        │
│                                    │
│ ┌────────────────────────────────┐ │
│ │ CHASE_PP marches down the      │ │  ← bottom, captioned
│ │ Adventure Bay sidewalk in his  │ │
│ │ police uniform, proud.         │ │
│ └────────────────────────────────┘ │
└────────────────────────────────────┘
                                                 (then 0.5s of pure black)
                                                 (then clip #0002, same overlay scheme)
```

### ffmpeg pipeline (two-pass)

**Pass 1 — render each clip with overlay** (same resolution + FPS for clean concat):

```bash
# For each clip i:
ffmpeg -y -i videos/0001.mp4 \
  -vf "scale=960:544:force_original_aspect_ratio=decrease,
       pad=960:544:(ow-iw)/2:(oh-ih)/2:black,
       drawtext=text='#0001':x=30:y=30:fontsize=64:fontcolor=white:
                box=1:boxcolor=black@0.6:boxborderw=12,
       drawtext=textfile=prompts/0001.txt:reload=0:
                x=(w-text_w)/2:y=h-110:fontsize=32:fontcolor=white:
                box=1:boxcolor=black@0.7:boxborderw=12:line_spacing=8" \
  -r 24 -c:v libx264 -preset fast -crf 20 -pix_fmt yuv420p \
  -an \                                            # gallery is silent — captions carry the meaning
  gallery/per_clip/0001.mp4
```

**Pass 2 — black inter-clip frame + concatenate** (stream copy, fast):

```bash
# One-time black filler, 0.5s @ 24fps, same encode settings
ffmpeg -y -f lavfi -i color=c=black:s=960x544:d=0.5:r=24 \
  -c:v libx264 -preset fast -crf 20 -pix_fmt yuv420p \
  gallery/per_clip/_black.mp4

# Build concat list (skip clips marked deleted in metadata.json):
cat > /tmp/concat.txt <<EOF
file 'per_clip/0001.mp4'
file 'per_clip/_black.mp4'
file 'per_clip/0002.mp4'
file 'per_clip/_black.mp4'
...
EOF

# Concat without re-encoding (fast)
ffmpeg -y -f concat -safe 0 -i /tmp/concat.txt -c copy gallery/gallery.mp4
```

### Why two-pass

- Pass 1 normalizes resolution + FPS + codec across heterogeneous source clips, so pass 2 can use `-c copy` (no re-encode → seconds, not minutes, for a 150-clip gallery).
- Each per-clip render is cached in `gallery/per_clip/`. Re-rendering after a deletion is one ffmpeg invocation (rebuild concat list, re-concat). No per-clip work repeats.

### Length sanity

150 clips × ~2–4s + 150 × 0.5s gaps ≈ **6–12 minutes total**. Skimmable.

### Resolution

Gallery clips render at **960×544 @ 24fps** to match the recommended training resolution (per `Phase0_ResolutionFPS.md`). If you'd rather see source resolution preserved (so artifacts caused by training-resolution downscale aren't masked in QC), I can flip pass 1 to `-vf "drawtext=...,drawtext=..."` only and skip the scale step — at the cost of needing pass 2 to re-encode. Quick to switch.

---

## 5. The "delete clip #42" workflow

```bash
python scripts/dataset_pipeline/manage.py delete \
  --dataset chase_golden_v1_20260428 \
  --index 42 \
  --reason "low quality, character off-model"
```

What happens, atomically:

1. Read `metadata.json`, find clip with `index=42`. Refuse if it doesn't exist or is already `status=deleted`.
2. Move (don't rm) `videos/0042.mp4`, `prompts/0042.txt`, `latents/0042.pt`, `gallery/per_clip/0042.mp4` into a top-level `_trash/{deleted_at}/` (also gitignored, GCS-side moves are server-side renames). Recoverable for 7 days; cron-cleanup later.
3. Update metadata.json: clip 42's `status` flips to `deleted`, `deletion` block populated.
4. Append a line to `audit/deletions.log` (date, index, reason, who).
5. Rebuild `prompts/all.csv` (active clips only).
6. Re-concat the gallery (one `ffmpeg -f concat`, ~5 seconds).
7. Print summary: `dataset chase_golden_v1: 137 active, 13 deleted (after this op).`

Bulk variant: `manage.py delete --indices 5,17,42,89-92` for ranges.

Inverse: `manage.py restore --index 42` (puts it back from `_trash/` if still there).

---

## 6. Build pipeline (`build_dataset.py`)

The script that creates a fresh dataset from inputs:

```bash
python scripts/dataset_pipeline/build_dataset.py \
  --manifest gs://video_gen_dataset/dataset/labelbox/chase_golden.json \
  --character chase \
  --version 1 \
  --target-resolution 960x544 --target-fps 24 --frames 49 \
  --brand-token CHASE_PP \
  --output gs://video_gen_dataset/training_data/chase_golden_v1_20260428/
```

Stages, all idempotent (re-run resumes from where it stopped):

| # | Stage | Where it runs | What it does |
|---|---|---|---|
| 1 | `fetch_manifest` | local | Download chase_golden.json + .parquet → cache locally with md5 verify |
| 2 | `extract_clips` | GPU VM (or anywhere with disk + bandwidth) | For each manifest entry: pull source episode mp4 from `raw/paw_patrol/`, ffmpeg-cut at `[t0, t1]`, scale to 960×544 @ 24fps, 49-frame trim, write `videos/0NNN.mp4` |
| 3 | `write_prompts` | local | Read caption from manifest, substitute character name → `CHASE_PP`, write `prompts/0NNN.txt` |
| 4 | `encode_latents` | **GPU VM** | Run trainer's preprocessing on each `videos/0NNN.mp4` → `latents/0NNN.pt` |
| 5 | `render_gallery` | local or VM (CPU-bound ffmpeg) | §4 pass 1 + pass 2 → `gallery/gallery.mp4` |
| 6 | `write_metadata` | local | Assemble `metadata.json` from all of the above (md5s, durations, dimensions, source provenance) |
| 7 | `upload` | local | rclone/gsutil push to `gs://.../training_data/{dataset_id}/` |

Each stage writes a `audit/build.log` line with stage name, start/end ts, output digest. Re-running `build_dataset.py` skips stages whose outputs already exist with matching md5 (provenance from previous run).

---

## 7. What gets thrown away on cutover

Per your "regenerate over salvage" rule:

| Old location | What it has | Plan |
|---|---|---|
| `gs://video_gen_dataset/TinyStories/data_sets/full_cast_combined_preprocessed/*.pt` | 1 563 preprocessed latents | **Discard.** Regenerated by stage 4 above. |
| `gs://video_gen_dataset/TinyStories/data_sets/full_cast_raw/*.{mp4,csv}` | 136 mp4s + 4 csvs | **Discard.** Regenerated from manifest + raw. |
| `gs://video_gen_dataset/TinyStories/data_sets/golden_skye/videos/*.mp4` | 636 mp4s | **Discard.** Regenerated. |
| `gs://video_gen_dataset/TinyStories/data_sets/phase4_phase1_chase_skye/` | 1 021 files | **Discard.** Phase 4 dataset gets its own `phase4_v1_*` build later. |
| `gs://video_gen_dataset/TinyStories/character_images_bank/*.jpg` | 138 anchor stills | **Discard.** Anchor stills regenerated by an `anchors_extract.py` (separate flow, see §9). |
| `gs://video_gen_dataset/dataset/labelbox/output/*.json` (22 669 files) | Per-frame Labelbox annotations | **Discard.** We don't use Labelbox per-frame annotations downstream. |
| `gs://video_gen_dataset/dataset/labelbox/videos/*.{mp4,wav}` | 4 721 paired clips | **Keep as input only** if a manifest still references them; otherwise the new pipeline pulls fresh from `raw/`. |

**Action on discard:** move to `gs://video_gen_dataset/_archive/2026-04-28/` (one-time snapshot) before any deletion. We never `rm` directly; we move and let lifecycle policy clean up after 30 days. You approve the archive move; the lifecycle policy deletes itself.

---

## 8. Tooling layout in this repo

```
scripts/dataset_pipeline/
    build_dataset.py          # §6 orchestrator
    manage.py                 # §5 delete / restore / list / verify
    gallery.py                # §4 ffmpeg gallery (callable standalone)
    schema.py                 # metadata.json dataclass + validators
    stages/
        fetch_manifest.py
        extract_clips.py
        write_prompts.py
        encode_latents.py
        render_gallery.py
        write_metadata.py
        upload.py
    fonts/
        Inter-Bold.ttf        # bundled to make drawtext deterministic across machines
```

`schema.py` is the canonical source for the `metadata.json` shape. Trainer + `manage.py` both depend on it. Linted via Pydantic or dataclasses-json.

---

## 9. Anchor stills — separate but parallel

Anchors are images, not clips, so they get their own slim pipeline:

```
gs://video_gen_dataset/training_data/{character}_anchors_v1_20260428/
    metadata.json
    images/
        0001.jpg
        ...
    gallery/
        contact_sheet.jpg     ← grid of all anchors with index labels (no video gallery, this is stills)
    audit/build.log
```

`scripts/dataset_pipeline/anchors_extract.py`:
- Pulls a list of (episode, frame_index) from a small input JSON (or random sampling from S-tier scenes).
- ffmpeg-extracts frames.
- Filters by sharpness + face-detection presence.
- Renders contact sheet via ImageMagick.

Same `manage.py delete --index N` semantics. Same metadata schema (with `images/` instead of `videos/`).

This is a v1.1 deliverable — not blocking the clip pipeline. Mention here for completeness.

---

## 10. Open questions before I start coding

Small, all answerable in one message:

1. **Where do I run `extract_clips` and `encode_latents`?** GPU VM (`35.238.2.51`) is the obvious place for stage 4 (latent encoding needs the trainer + GPU). Stage 2 (ffmpeg clip extraction) can run anywhere; running it on the same VM avoids streaming 494 GB of raw episodes to a laptop. Confirm I should plan for: stages 1, 3, 5, 6 local; stages 2, 4, 7 on the VM.

2. **Captions: which field in `chase_golden.json` is the caption?** I haven't opened the manifest yet. Once I do, I'll show you the schema and confirm — if there's a `caption` field we use it directly; if there are multiple variants (`caption_orig`, `caption_recaptioned`), you pick which.

3. **Brand-token substitution scope.** When a manifest caption says "Chase walks down the street" — do I rewrite to `CHASE_PP walks down the street` *during build*, or feed the model literal `Chase` and let the LoRA bind to that? My recommendation from the tokenizer probe (`Phase0_BrandTokens.md`) was `CHASE_PP` for Option A. Confirm we apply that substitution at build-time so the saved `prompts/*.txt` already contain `CHASE_PP`.

4. **Font for drawtext.** Bundle `Inter-Bold.ttf` (free, clean, dense) or use a system font? Bundling avoids "looks different on Mac vs VM."

5. **Initial dataset to build.** `chase_golden_v1` first, `skye_golden_v1` second? Or both in parallel since they share zero data?

If you answer those — even briefly — I'll start with §8 scaffolding + §6 build pipeline, ending with the gallery video for `chase_golden_v1` so you have something to watch and react to.

---

## 11. What this replaces in the existing plan

- Phase 0 reports `Phase0_QualityReport.md` and `Phase0_AnchorGap.md` are now **obsolete** — they were measuring the wrong pool. Mark them deprecated; the gallery video replaces them as the QC artifact.
- `Phase0_BrandTokens.md` and `Phase0_ResolutionFPS.md` are still load-bearing — their conclusions (`CHASE_PP`/`SKYE_PP`, 960×544 @ 24fps, 49 frames) are inputs to this pipeline.
- `docs/training_data.md` (the live map) gets a new section pointing to the rebuilt datasets once they exist; old GCS prefixes stay in the map under "archive candidates" until you confirm cleanup.
- The §5 target consolidation layout I proposed yesterday is **superseded** by §2 here. Same spirit, more concrete.
