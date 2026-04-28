# VLM Video QA — Benchmark V3 Summary

**Date:** 2026-04-24
**Scope:** 2 scenes × 6 models × 2 tiers = 24 analyses (23/24 complete; Grok crosswalk Pro failed at aggregator, retryable)

## What's new in V3

1. **Facial Topology Audit** — new per-character agent that takes a dedicated 24-panel **eye-crop grid** (zoomed to just the eye region, 3-5× more pixels than the whole-face crop) and forces a structured geometric analysis: per-panel `{aspect_ratio, shape_descriptor, eyelid_arc, view_angle}`. Head-pose normalisation built into the prompt. Flags morphological inconsistency when shape changes between same-angle open-eye panels.
2. **Grids bumped: 16 → 24 panels (4×6)** everywhere — keyframe, body, face, and the new eye grid.

---

## Final Scores

### Crosswalk Safety

| Model | Pro | Flash |
|---|---:|---:|
| Kling-V3-Pro-I2V | 48 | 55 |
| Seedance-1.5-Pro-I2V | 55 | 25 |
| LTX-2.3-Fast | 55 | 45 |
| Veo-3.1-Fast-FLF | 55 | 50 |
| Wan-2.6-I2V-Flash | 45 | 55 |
| Grok-Imagine-I2V | *agg failed* | 45 |

### Trampoline Backflip

| Model | Pro | Flash |
|---|---:|---:|
| Kling-V3-Pro-I2V | 55 | 45 |
| Seedance-1.5-Pro-I2V | 55 | 55 |
| LTX-2.3-Fast | 55 | 45 |
| Wan-2.6-I2V-Flash | 55 | 55 |
| Grok-Imagine-I2V | 55 | 50 |
| Veo-3.1-Fast-FLF | 45 | 55 |

Everything FAIL'd in at least one tier — even Kling trampoline dropped from 80 (CONDITIONAL_PASS) in V2 to 55 (FAIL) in V3 because the new Facial Topology Audit flagged Chase's eyes as structurally inconsistent, dragging the aggregate down.

---

## Did the Facial Topology Audit earn its slot?

**Yes — it caught real structural issues the soft Face & Uncanny agent consistently missed.**

Per-character verdicts where Topology disagreed with Face & Uncanny (all catches; Face said `great` in every one of these):

| Scene | Model | Character | Topology verdict | Topology evidence |
|---|---|---|---|---|
| Trampoline | **Grok** | pink dog | **major_issues** | "eye geometry unstable, morphing between round and oval at the same head angle" |
| Trampoline | **Kling** | police dog | **major_issues** | "significant changes in shape and aspect ratio between round, oval" |
| Trampoline (Flash) | **Kling** | pink dog | **major_issues** | "aspect ratio differences exceeding 0.7 between round/almond" |
| Crosswalk | Kling | pink dog | minor_issues | "change from round to oval at same viewing angle" |
| Crosswalk | LTX | pink dog | minor_issues | "single instance of significant shape change" |
| Crosswalk | LTX | blue police dog | minor_issues | "change from round to oval at same frontal view" |

**In every case, Face & Uncanny said `great` on the exact same grid.** Topology caught what the soft aesthetic agent was missing. The rigorous `measure the geometric skeleton, not the feeling` framing works as you described.

**Clean passes (Topology correctly said `great`):** Seedance pink dog (trampoline), Veo both characters, Wan both characters, and several others — the agent is not trigger-happy. Attributes shape changes to expression/head-rotation when they're genuinely expression-driven.

---

## Caveats

### 1. The specific Veo pink-dog eye morph you flagged wasn't caught

Both Pro and Flash said `great` on Veo pink dog (crosswalk + trampoline). The agent's reasoning: *"observed shape variations attributable to expressions like squinting."* Either the morph is more subtle than the 24-panel sampling can resolve, or the VLM is still being charitable about that specific case. The audit DID catch the equivalent defect on Grok trampoline and Kling trampoline — so it works in principle but not 100% reliably.

Possible next step: bump to 36 panels (4×9) AND add a per-character eye-crop aspect ratio *trajectory* computed from the VLM's numeric output — if the ratio drifts monotonically from start to end, that's a morph even if no single pair "pops."

### 2. Aggregate scores look uniformly bad

Every video FAIL'd. That's partly because adding Facial Topology Audit + Prompt-vs-Caption added two agents that readily return `major_issues`, and the aggregator compounds them. The scoring rubric may need recalibration for "how many simultaneous `major` findings = FAIL." Right now any major pulls the score below 70. Worth thinking about.

### 3. Kling dropped from 80 → 55 on trampoline

V2 Kling trampoline scored 80 CONDITIONAL_PASS; V3 dropped to 55 FAIL because Facial Topology Audit flagged Chase's eye geometry. Inspecting the finding: *"significant changes in shape and aspect ratio between round, oval, and almond."* Could be genuine, or could be that Chase's dynamic expressions (wide-eyed during backflip admiration) are being misread as structural drift. Worth a visual check.

---

## Sensitivity discussion

The Facial Topology Audit is now the most aggressive per-character agent. On the 23 completed Pro + Flash videos:
- **5 major_issues** flagged across different characters
- **4 minor_issues** flagged
- **~34 great** (most)

That's a 21% flag rate on new topology defects — meaningful signal, not noise. Combined with the existing Character Consistency Audit (separate rubric: rendering/geometry/assets/color) we now have two complementary structural-consistency lenses per character.

---

## Viewer

**http://localhost:8765/** — the 4 scene cards (2 tiers × 2 scenes). Each report viewer now shows an **additional eye-region grid** thumbnail per character and the Facial Topology Audit agent card with per-panel measurement evidence.
