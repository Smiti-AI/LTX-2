# Paw Patrol Vertical Engine — Project Vision

> This document is **context, not instruction.** It exists so that anyone reading the active plan understands where things are headed and why. When this document and `active_plan.md` disagree, **active_plan.md wins.**

---

## Mission

Build a generative video Vertical Engine — a customized model stack — for premier children's brands, starting with Paw Patrol. The engine operates under strict Brand Book rules covering aesthetics, character dynamics, motion, and audio identity. The standard is 100% Brand Alignment, not "pretty good for an AI."

Foundation model: **LTX-2 (Lightricks)**. All work layers on top of LTX-2 via LoRA and adjacent fine-tuning techniques.

Repos:
- https://github.com/Lightricks/LTX-2
- https://github.com/Lightricks/LTX-Video-Trainer

## What "Done" Eventually Looks Like

- High-quality single shots (15–30 seconds) featuring named characters with precise dialogue and original sound.
- Long-term: full short-story generation, multi-shot continuity, multiple characters interacting.
- Output is indistinguishable from official brand content to brand reviewers.

## The Four-Step Arc

The project has four sequential training phases. Three are currently frozen; we work on Step 1 only until visual stability is confirmed.

### Step 1 — Visual Baseline (active)
Anchor character appearance, anatomy, brand details (badges, collars), and brand-specific motion. Solve catastrophic forgetting that LoRA introduces against the base LTX-2 model. Strategy: 3-tier dataset (static / dynamic / closeup) + brand tokens + high-res anchor stills.

### Step 2 — Audio-Centric Training (frozen)
Authentic character voices with zero vocal distortion. LoRA focused on cross-attention layers. Verbatim Whisper transcription including filler sounds (Ah, Oh, Phew) for accurate lip-sync. 80 single-speaker clips, 50% close-up face shots during speech.

### Step 3 — Dialogue Training (frozen)
Multi-character interaction without voice bleeding. Strict turn-taking captions. Validation: lip movement only on the active speaker. 50 dual-speaker interaction clips.

### Step 4 — Script Fidelity (frozen)
Verbatim instruction following — what the prompt says is what the character says. Synthetic caption variations, long-form sequences (up to 30s), DPO with negative examples. Final benchmark (BM_v1) measures script-to-output accuracy.

**The freeze rule:** no work on Steps 2–4 — no data prep, no scaffolding, no exploration — until Step 1 produces a visual baseline that passes the human-eval scorecard.

## Brand Alignment Philosophy

Generic models trained on the open web "know" what a German Shepherd looks like, what a Cockapoo looks like, what a children's cartoon looks like. They don't know Chase. They don't know Skye. They will blend brand characters with generic concepts unless we explicitly create separate representational space for them.

Two principles follow:

**1. Brand tokens create their own drawer.** Using a unique token (final format determined empirically in Phase 0) tells the model "this is a new concept, not a flavor of an existing one." The base model's general knowledge stays intact; brand-specific knowledge gets its own dedicated representation.

**2. Three-tier data prevents catastrophic forgetting.**
- **Static excellence (50 clips):** 2–3s near-still shots (blinking/breathing) that anchor visual weights and textures.
- **Dynamic brand (70 clips):** 3–5s cinematic motion that captures character-specific movement (Chase's authoritative march, Skye's bouncy lift).
- **High-detail closeups (30 clips):** faces and paws to fix the distortions LoRA tends to introduce.

Plus **500 high-res stills as anchors** to maintain rendering fidelity while motion is learned from video.

## Gaps We're Closing

What "100% brand alignment" means concretely:

- **Visual artifacts** — eliminate glitches and eye distortions (these worsened after initial LoRA training vs. base model).
- **Anatomy & details** — body structure (paws, tails), brand-specific markings (badges, collar symbols).
- **Paw Patrol style** — the aesthetic itself, not just character likeness.
- **Audio & dialogue** — speech cadence, character-specific timbre.
- **Instruction following** — verbatim adherence to prompted dialogue.
- **Physics & weight** — character-specific motion (Chase moves with authority; Skye is light and bouncy). Generic models feel "floaty."

## Validation Identity — The Iconic Shots

Three validation prompts are locked for the life of the project. They render at every milestone. Their stability over time matters more than perfection of selection:

- **Chase's Authoritative March** — straight line, Adventure Bay sidewalk, exaggerated proud police march. Audio: *"Chase is on the case!"* (commanding tone).
- **Skye's Bubblegum** — blows a giant pink bubble that pops on her face; opens one eye, smiles, speaks. Audio: *"Let's take to the sky!"* (enthusiastic tone).
- **The Acrobatic Sneeze** — fly lands on nose, pupils magnetize to the center, powerful sneeze results in a backflip.

## Success Discipline

The project lives or dies by reproducibility. Specific commitments (binding from day one):

- Every experiment has a README explaining *why* it was run before it starts, and what was learned after.
- Every metrics dashboard logs **loss-per-sigma, validation loss, and total training loss** — not just aggregate.
- Every caption set is versioned and traceable to the model checkpoints it produced.
- Storage retention is aggressive: middle + final checkpoints only. Reconstruction from documentation > hoarding.
- Decisions get ADRs. Six months from now, "why did we pick X" should always have a written answer.

## Scope of This Document

Vision, philosophy, the full arc. The tactical "what to do this week" lives in `active_plan.md`. If you find yourself referencing this document for an instruction, you're using it wrong.
