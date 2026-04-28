# Paw Patrol Vertical Engine — Active Plan

> **This file binds.** Anything not in this file is context, not instruction.
> See `vision.md` for the long arc. When this file and `vision.md` disagree, **this file wins.**

---

## Locked Decisions

| Decision | Choice |
|---|---|
| Brand alignment evaluation | Human eval scorecard (1–5 per category, no automated metrics for now) |
| High-res anchor stills | Partial set exists; gap must be sourced/extracted as a sub-project |
| Catastrophic forgetting strategy | 3-tier data + brand tokens + high-res anchors (per `vision.md`) |
| Data pipeline architecture | Hybrid — scripts for captioning, agent for QC/review |
| Data legality | Cleared. Proceed without rights-checking gates. |
| Project freeze | Steps 2–4 (audio, dialogue, script) frozen until Step 1 baseline approved |
| GCP auth | Always-on. Set up once at session start; never re-prompt mid-task. |

If you (Claude Code) believe one of these is wrong, **raise it explicitly before silently routing around it.**

---

## Current Scope

Two things are in scope right now:

1. **Pre-flight + GCP auth** (§0 and §1).
2. **Phase 0 — Discovery & Foundation** (§3).

Phase 1 (MLOps scaffolding) and beyond will be planned **by Claude Code** after Phase 0 reports land. The user will review those plans before they become binding.

---

## §0 — Pre-Flight Checklist

The first action in a new session is to request the items below as a single batch and write the responses to `pre_flight.yaml`. Refuse to start substantive work until each item is filled in or explicitly marked `skip`.

**Credentials & access**
1. GCP project ID and the bucket(s) to audit.
2. Service account key path, or confirmation ADC is set up.
3. W&B API key + project name + entity.
4. Gemini API key.
5. Whisper choice — hosted (API key) or local large-v3.
6. Hugging Face token (if needed for LTX-2 assets).
7. GitHub repo URL (or "create new") + auto-commit policy.

**Data locations**
8. Every directory or bucket where Paw Patrol assets might live.
9. Path to the existing 150-clip "Golden Set," if known.
10. Path to existing high-res stills (the partial set).

**Environment**
11. Local GPU spec (VRAM, count). Training target: local, GCP, or both.
12. Python/CUDA versions and the path to the existing LTX-Video-Trainer clone.

**Conventions**
13. Branch strategy (trunk-based vs. feature branches).
14. Permission to install system-level packages.

Items left as `TBD` must be tracked. Any downstream task blocked by a `TBD` must say so explicitly when proposed.

---

## §1 — Always-On GCP Authentication

Goal: never ask the user to run `gcloud auth login` mid-task again.

- Install gcloud CLI if missing.
- Run `gcloud auth application-default login` once with the user; verify with `gcloud auth application-default print-access-token`.
- Prefer service account JSON for long-running jobs. Set `GOOGLE_APPLICATION_CREDENTIALS` in `.envrc` (direnv) or the shell rc.
- Create `scripts/preflight_gcp.sh` that checks auth + project + bucket access. Run at the start of every session.
- Document setup in `docs/auth.md`.

After this, GCP is ambient. No re-prompting.

---

## §3 — Phase 0: Discovery & Foundation

No training. No assumptions. Surface ground truth before designing anything else.

### 3.1 Data Audit Script

Walks every path/bucket from pre-flight. Output: `data_inventory.csv`.

**Columns:**
`path, type (video|image|caption|checkpoint), duration_s, width, height, fps, codec, file_hash (sha256), size_mb, last_modified, guessed_character, guessed_scene_type, source_episode_guess, notes`

Heuristics for `guessed_*` come from filename + folder structure only. **No VLM calls in Phase 0.**

The hash is critical — previously used data is almost certainly duplicated across folders.

**Acceptance:** running the script twice produces an identical CSV (deterministic). Output covers 100% of pre-flight paths.

### 3.2 Quality Triage Report

For every clip flagged Paw-Patrol-relevant in the audit, compute:
- Sharpness (Laplacian variance).
- Letterboxing detection (edge histogram).
- Motion magnitude (optical flow stats).
- Watermark detection (template match against known PP logo positions).
- Audio presence + estimated SNR.

**Outputs:**
- `quality_triage.csv` — per-clip scores.
- `Phase0_QualityReport.md` — histograms + recommendation on which clips clear the bar for Static / Dynamic / Closeup buckets, and where we're short of the 50/70/30 target.

### 3.3 Resolution & FPS Analysis

**Output:** `Phase0_ResolutionFPS.md` answering:
- Native resolution distribution of source clips (histogram).
- Native FPS distribution (24 / 30 / other?).
- What was LTX-2 trained on? Pull from the Lightricks repo / docs and cite.
- For a 9:16 vertical target, trade-offs between cropping vs. pillar-boxing vs. content-aware reframing.
- Recommended training resolution + FPS, with reasoning.
- A "what we'd give up if we chose differently" section.

### 3.4 Brand Token Format — Empirical Research

LTX-2's text encoder determines whether a brand token gets a clean dedicated representation or gets shattered into subwords overlapping with generic concepts. Don't guess — test.

**Tasks:**
1. Identify LTX-2's exact text encoder (read the model config in the repo).
2. Write a short script that tokenizes each candidate and prints token IDs + decoded subwords:
   - `CHASE_PP`, `SKYE_PP`
   - `<chase_pp>`, `<skye_pp>`
   - `[CHASE]`, `[SKYE]`
   - `chasepawpatrol`, `skyepawpatrol`
   - `sks chase`, `sks skye`
   - True new tokens added to the tokenizer (note: requires tokenizer + embedding-matrix modification).
3. Check what LTX-Video-Trainer's docs/examples recommend.
4. Survey 2–3 recent video-LoRA repos (Dreambooth-LoRA, AnimateDiff-LoRA, etc.) for what they did and why.

**Output:** `Phase0_BrandTokens.md` — candidate table, tokenization behavior, pros/cons, and **one recommendation** with reasoning.

**Constraint:** the recommendation must be implementable without modifying the LTX-Video-Trainer's core code if at all possible. If the best option requires tokenizer modification, flag as a separate engineering task needing approval.

### 3.5 Anchor Stills Inventory & Gap Plan

Since some stills exist and some don't:

**Output:** `Phase0_AnchorGap.md`:
- What stills already exist (from the audit).
- What's missing to reach the 500 target, broken down by character (Chase / Skye) and shot type (face / paws / full body / iconic poses).
- Recommended extraction methodology for the gap (keyframe extraction from S-tier clips, optional upscaling pipeline).
- Becomes a Phase 2 sub-task after the gap is quantified.

---

## MLOps Principles (binding from day one)

These are not Phase 1 implementation details — they are commitments that bind any future scaffolding plan you propose:

- **Hierarchical directory structure** with clear separation: `/data`, `/experiments`, `/scripts`, `/eval`, `/docs`.
- **Experiment log:** `experiment_log.csv` tracks every run with hyperparameters, W&B link, results.
- **W&B logging always includes:** loss-per-sigma, validation loss, total training loss, sampled validation generations.
- **Checkpoint retention:** middle + final only. Aggressive deletion of intermediate checkpoints.
- **Caption versioning:** versioned folders (`v{N}_{YYYYMMDD}_{slug}/` + `manifest.json`). Every model checkpoint traceable to its caption version.
- **Human eval scorecard system:** real infrastructure (rubric + blinded comparison + CSV storage), not ad-hoc opinions.
- **ADRs:** every meaningful decision gets a numbered ADR in `docs/decisions/` (Context / Decision / Consequences).
- **Reproducibility README per experiment:** the "why" before the run, the "what was learned" after.

You will propose the Phase 1 implementation that delivers on these principles **after Phase 0 reports land.**

---

## Iconic Validation Shots (locked for project life)

Render at every milestone. Comparability over time matters more than perfect shot selection:

1. **Chase's Authoritative March.** Straight line, Adventure Bay sidewalk, proud police march. Audio: *"Chase is on the case!"*
2. **Skye's Bubblegum.** Giant pink bubble pops on face; opens one eye, smiles, speaks. Audio: *"Let's take to the sky!"*
3. **The Acrobatic Sneeze.** Fly lands on nose, pupils magnetize, sneeze results in a backflip.

---

## Open Questions (raise when relevant; do not silently assume)

Not blocking Phase 0, but flag if any become relevant:

- **Rater pool for human eval:** just the user? user + teammate? brand reviewers ever?
- **Compute target:** local, GCP, both? Affects whether cloud training scripts are needed in Phase 1.
- **Threshold to unfreeze Phase 2 (audio):** specific scorecard score, or qualitative judgment?
