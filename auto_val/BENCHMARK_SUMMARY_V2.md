# VLM Video QA — Benchmark V2 Summary
**Date:** 2026-04-24
**Scope:** 2 scenes (crosswalk_safety, trampoline_backflip) × 6 models × 2 tiers (Pro, Flash) = **24 analyses**

This iteration ships **three major architectural changes** addressing the defects you flagged in V1.

---

## The three changes under test

| # | Change | Replaces |
|---|---|---|
| **1** | Classical optical-flow lip-sync (Farneback dense flow on mouth region) | VLM Lip-Sync agent (high false-alarm rate) |
| **2** | Homography-stabilised keyframe grid for Environment Stability (ORB+RANSAC, character bboxes masked out) | Raw keyframe grid (couldn't distinguish camera-pan from teleport) |
| **3** | Blind Captioner → Prompt-vs-Caption comparator (text-only deduction, no priming bias) | *Added alongside* Prompt Fidelity (not a replacement) |
|   | Bonus: per-character grids bumped **16 → 24 panels (4×6)** for slow-drift catch | — |

---

## Full score table

### skye_chase_crosswalk_safety

| Model | Pro | Flash | Δ |
|---|---:|---:|---:|
| Veo-3.1-Fast-FLF | 65 | 65 | 0 |
| Kling-V3-Pro-I2V | 55 | 55 | 0 |
| Seedance-1.5-Pro-I2V | 55 | 60 | +5 |
| Wan-2.6-I2V-Flash | 55 | 55 | 0 |
| **LTX-2.3-Fast** | 55 | **95 (PASS)** | **+40** ⚠ |
| Grok-Imagine-I2V | 45 | 55 | +10 |

### skye_chase_trampoline_backflip

| Model | Pro | Flash | Δ |
|---|---:|---:|---:|
| **Kling-V3-Pro-I2V** | **80 (CP)** | **78 (CP)** | −2 |
| Seedance-1.5-Pro-I2V | 65 | 45 | −20 |
| Wan-2.6-I2V-Flash | 62 | 45 | −17 |
| Grok-Imagine-I2V | 55 | 45 | −10 |
| Veo-3.1-Fast-FLF | 55 | 55 | 0 |
| LTX-2.3-Fast | 52 | 35 | −17 |

---

## Did the three changes work? — item-by-item audit against your V1 feedback

### ✅ **Change 1: Lip-Sync false positives — FIXED**

**V1 failure (your feedback):** Pro and Flash both flagged "major lip-sync defects" on Kling crosswalk where lip-sync was actually fine. Same for Veo. Hallucinations.

**V2 result:** Classical optical-flow lipsync is **deterministic and evidence-based**. Kling crosswalk output:

```
All 4 speech segments show clear mouth animation (mean flow > 0.6 px/frame).
  Segment [0.00-2.46s] "We remember the first rule…": 13.91 px/frame (MOVING)
  Segment [2.46-4.00s] "Friends, this is very important.":  9.66 px/frame (MOVING)
  Segment [4.00-7.34s] "Always you cross crosswalk…":     13.40 px/frame (MOVING)
  Segment [7.34-10.00s] "And only when the road is…":     13.51 px/frame (MOVING)
```

**All 12 Pro videos across both scenes: lipsync verdict = `great`** (when the mouth is actually moving, flow values are consistently 5-15 px/frame; the 0.6 threshold correctly lets them pass). Zero hallucinated false-positives.

**Caveat — possible regression on the Wan case:** V1, the VLM lipsync correctly flagged Wan crosswalk as broken (per your confirmation). V2 classical flow says Wan crosswalk is fine (mean flow ~10 px/frame during speech). This suggests the mouths ARE moving physically, but may not be matching phonemes. Classical flow measures motion magnitude, not phoneme accuracy. If this matters, we'd need a more sophisticated signal (e.g. SyncNet). Worth investigating with the specific Wan file.

### ✅ **Change 2: Environment Stability camera-pan false positives — PARTLY FIXED**

**V1 failure (your feedback):** Wan crosswalk — yellow arrow sign flagged as "appearing mid-clip." False positive; the camera just panned to reveal it.

**V2 result with homography stabilisation:**

```
Wan crosswalk Environment Stability: [great] No defects observed in this scope.
"A review of the background across all 16 keyframe panels revealed no
 world-permanence defects. Static background elements, including the
 buildings, the tower on the hill, the street sign, the benches, and
 the parked vehicles in the distance, all remained stable and in their
 correct world positions throughout."
```

**The stabilised grid correctly suppressed the camera-pan false positive.** 15 of 15 frames were successfully registered via ORB+RANSAC homography with character regions masked out.

### ❌ **Change 2 weak case — Seedance trampoline sign detachment STILL MISSED**

**V1 failure (your feedback):** Seedance trampoline — a street sign physically detaches and moves ~1m forward in the scene. Real defect. Agents missed it.

**V2 result:** still missed.
```
Seedance trampoline Environment Stability: [great] No defects observed in this scope.
```

**Why it's still being missed:** The homography is too forgiving — it successfully registers the frames *because* the sign detachment is gradual and small-ish. The registered view makes the sign look consistent rather than drifting. This is a limit of the approach: stabilisation only exposes teleports that a homography can't absorb. To catch slow drift like this we'd need residual motion analysis in the background region (e.g., per-pixel flow after stabilisation), which is a further CV step.

### ✅ **Change 3: Blind Captioner + Prompt-vs-Caption — VALUABLE ADDITION**

**Direct comparison on specific videos:**

**Kling crosswalk:**
- Prompt Fidelity (primed): `great` — missed the issues ⚠
- Prompt vs Caption (blind): `major_issues` — caught character-positioning and action contradictions ✓

**Veo crosswalk ("look left/right" missing action from V1):**
- Prompt Fidelity: caught it — *"characters look at each other rather than looking left and right down the street"*
- Prompt vs Caption: also caught it, but focused on **different issues** (attire details: "goggles" vs "hat", body posture specifics)

**The two agents are complementary.** Prompt Fidelity catches speaker-assignment and action-execution failures. Prompt vs Caption catches concrete attribute mismatches (outfit, body shape, scene layout). Keeping both is justified.

### ⚠ **Change (bonus): 24-panel per-character grids — PARTIAL WIN**

**V1 failure (your feedback):** Veo pink dog — eyes morph round → almond over the clip. 16 panels missed it.

**V2 result:** The 24-panel Face & Uncanny agent **still says `great`** on Veo pink dog. BUT the **Character Consistency Audit** (strict judge rubric) did pick up anatomy warping: `aggregate drift 13/40 — minor_issues`, citing *"the character's face is severely distorted and warped by motion blur in multiple panels (e.g., #5), making the underlying geometry appear inconsistent with the reference."*

So Audit caught anatomy drift, but Face & Uncanny specifically didn't flag the eye-shape change. The eye-morph-specific detection still doesn't work well; it may require either explicit "eye-only" sub-crops or a dedicated eye-shape tracker.

---

## Summary — what's better, what's still broken

### Definitively better
- **Lip-sync**: no more hallucinations. Deterministic, numeric, cheap.
- **Camera-pan false positives on background stability**: cleared for Wan.
- **Blind Captioner / Prompt vs Caption**: finds outfit & layout discrepancies the primed Prompt Fidelity misses.

### Still weak
- **Slow object drift** (Seedance sign detachment) — stabilisation absorbs it rather than exposing it. Would need post-stabilisation residual-flow analysis.
- **Eye-shape morphing** (Veo pink dog) — Face agent still says "great" on these. Needs dedicated eye-landmark tracking.
- **Classical lipsync phoneme accuracy** — flow magnitude ≠ phoneme sync. May under-detect subtle TTS/mouth mismatches.

### Cost / time (per video)
- Pro tier: ~$0.30–0.40, wall-clock ~90–120 s
- Flash tier: ~$0.03–0.06, wall-clock ~60–90 s
- Additional cost of Blind Captioner + Prompt vs Caption: +~$0.04 per video. Environment stabilisation: 0.5 s CPU, no API. Classical lipsync: 6 s CPU, no API.

---

## Viewer

**http://localhost:8765/** — the split landing page. Both tiers, both scenes, 24 total videos.

Each report now shows:
- The NEW stabilised keyframe grid
- A raw (unstabilised) keyframe grid for comparison
- 24-panel body + face grids **per detected character**
- Per-character agent cards
- NEW sections: Blind Captioner, Prompt vs Caption, Lip-Sync (optical flow)

Open any model's viewer to see the lip-sync per-segment flow magnitudes, the stabilisation-registered frames, and the side-by-side primed-vs-blind prompt analyses.
