"""
Agents — specialist VLM evaluators. Each is a single Gemini call on the
native video plus optional evidence images / text context.

Design:
- Every agent receives the full .mp4 so Gemini hears audio and sees all frames.
- Some agents additionally receive a cropped grid (body, face, or speech)
  because static side-by-side panels defeat the VLM's playback-averaging bias.
- Every agent has a narrow scope, anti-hallucination guards, and defaults to
  `great` when no defect is observed.

Every paid call goes through `core.llm.call_model` — the chokepoint that
enforces mode-based model selection and records cost. Public functions take
`ctx: RunContext` as a keyword-only argument; the runner in `run.py` builds
one RunContext per video and passes it through.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, asdict
from typing import Optional

from google.genai import types

from .core import (
    ModelRole,
    RunContext,
    call_model,
)
from .core.llm import prewarm_client  # re-exported for run.py


@dataclass
class AgentReport:
    agent_id: str
    title: str
    focus: str
    verdict: str               # 'great' | 'minor_issues' | 'major_issues'
    summary: str
    findings: str
    elapsed_s: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# Output contract
# ─────────────────────────────────────────────────────────────────────────────

_OUTPUT_CONTRACT = """
CRITICAL OUTPUT RULES:

1. Respond in this exact JSON schema, nothing else:
{
  "verdict": "great" | "minor_issues" | "major_issues",
  "summary": "<one sentence. if no defects, literally say so.>",
  "findings": "<evidence-grounded paragraph. Only state things you directly observed. Quote dialogue verbatim.>"
}

2. GROUNDING: every claim must reference what you actually saw or heard.
   Do NOT invent defects. Do NOT speculate outside this agent's scope.

3. DEFAULT TO 'great': if nothing is wrong in your scope, verdict is 'great'
   and the summary is "No defects observed in this scope." Padding is failure.

4. NO TIMESTAMPS, NO NUMERIC METRICS, NO PIXEL COORDINATES. Describe naturally
   (e.g. "as the character lands", "in panel 4 of the grid").

5. When referencing a keyframe grid, cite the panel number (e.g. "in panel 4
   the character's left leg is shorter than in panel 2").
""".strip()


# ─────────────────────────────────────────────────────────────────────────────
# Generic runner
# ─────────────────────────────────────────────────────────────────────────────

def _call(
    system_prompt: str,
    video_bytes: bytes,
    extra_text: str = "",
    image_bytes: Optional[bytes] = None,
    image_caption: str = "",
    *,
    ctx: RunContext,
    role: ModelRole,
    agent_id: str,
) -> dict:
    parts: list = [types.Part.from_bytes(data=video_bytes, mime_type="video/mp4")]
    if image_bytes is not None:
        if image_caption:
            parts.append(image_caption)
        parts.append(types.Part.from_bytes(data=image_bytes, mime_type="image/png"))
    body = system_prompt.strip() + "\n\n" + _OUTPUT_CONTRACT
    if extra_text:
        body += "\n\n=== ADDITIONAL CONTEXT ===\n" + extra_text
    parts.append(body)

    resp = call_model(
        ctx=ctx,
        role=role,
        agent_id=agent_id,
        contents=parts,
        config=types.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json",
            http_options=types.HttpOptions(timeout=240_000),
        ),
    )
    return _parse_json((resp.text or "").strip())


def _parse_json(text: str) -> dict:
    if not text:
        return {"verdict": "great", "summary": "(empty response)", "findings": ""}
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        text = m.group(0)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return {"verdict": "great",
                "summary": "(unparseable model output — treated as clean)",
                "findings": text[:2000]}
    verdict = str(obj.get("verdict", "great")).lower().strip()
    if verdict not in {"great", "minor_issues", "major_issues"}:
        verdict = "great"
    return {
        "verdict": verdict,
        "summary": str(obj.get("summary", "")).strip(),
        "findings": str(obj.get("findings", "")).strip(),
    }


def _run(
    agent_id, title, prompt, video_bytes, extra="", image_bytes=None,
    image_caption="", *, ctx: RunContext, role: ModelRole,
):
    t0 = time.time()
    data = _call(
        prompt, video_bytes, extra, image_bytes, image_caption,
        ctx=ctx, role=role, agent_id=agent_id,
    )
    return AgentReport(
        agent_id=agent_id,
        title=title,
        focus=prompt.split("\n")[0].strip(),
        verdict=data["verdict"],
        summary=data["summary"],
        findings=data["findings"],
        elapsed_s=round(time.time() - t0, 1),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Agent 1 — Body Consistency
# ─────────────────────────────────────────────────────────────────────────────

def run_body_consistency(
    video_bytes: bytes,
    body_grid: bytes,
    character_name: str = "the primary character",
    *,
    ctx: RunContext,
) -> AgentReport:
    prompt = f"""
You are a continuity supervisor. Your scope is the CHARACTER BODY ONLY
for **{character_name}** — NOT faces, NOT motion, NOT lip-sync.

You have the full video AND a 4×4 BODY-CROP GRID (16 panels, each cropped
to **{character_name}**'s body across the clip). Compare panel-by-panel:

A. IDENTITY CONSISTENCY — Is the SAME character in every panel? Species,
   body shape, fur / skin color, clothing, accessories (vest, badge, hat,
   collar)? Any panel where the character's design morphs or swaps?

B. ANATOMY & PROPORTIONS — In each panel, is the body plausible and
   consistent with the others? Is a limb stretched, missing, duplicated,
   or the torso warped? Any panel where the body dissolves into a smear
   beyond reasonable motion blur?

IMPORTANT: cite panel numbers when flagging defects (e.g. "in panel 9 the
torso stretches unnaturally; in panels 1-8 the body looks consistent").

Out of scope: face quality (separate agent), eyes (separate agent), lip-sync
(separate agent), motion naturalness (separate agent).

If no identity or anatomy defects, verdict 'great'.
""".strip()
    agent_id = f"body_consistency::{character_name}"
    title = f"Body Consistency — {character_name}"
    r = _run(
        agent_id,
        title,
        prompt,
        video_bytes,
        image_bytes=body_grid,
        image_caption=f"BODY-CROP GRID — 16 panels of {character_name}'s body (panels 1-16):",
        ctx=ctx,
        role=ModelRole.CHARACTER_AGENT,
    )
    r.focus = f"Body consistency for {character_name}"
    return r


# ─────────────────────────────────────────────────────────────────────────────
# Agent 2 — Face & Uncanny Valley
# ─────────────────────────────────────────────────────────────────────────────

def run_face_uncanny(
    video_bytes: bytes,
    face_grid: bytes,
    character_name: str = "the primary character",
    *,
    ctx: RunContext,
) -> AgentReport:
    prompt = f"""
You are a character-animation director specialising in FACIAL performance.
Your scope is the FACE of **{character_name}** only. Use the provided 4×4
FACE-CROP GRID (16 panels, each tightly zoomed on **{character_name}**'s
face across the clip) plus the video.

Evaluate the face on four dimensions:

A. EYES ALIVE vs DEAD — Do the eyes blink across the clip? Do they change
   direction / gaze? Are they glassy, empty, or "staring-through-you"?
   Dead eyes across all panels = major issue.

B. EXPRESSION CHANGE — Do facial expressions shift with the emotional beats
   of the scene? Or is the face frozen in one pose across every panel?
   A face locked in one expression for the whole clip = major issue.

C. FACE ANATOMY — Are the eyes / mouth / nose positioned correctly and
   sized plausibly in every panel? Any panel where features melt, shift
   asymmetrically, or look mis-aligned?

D. UNCANNY VALLEY — Does the face feel subtly 'wrong'? Too-wide smile, eyes
   that don't track, rigid or waxy appearance, uneven mouth animation? Flag
   specific reasons, not just 'it feels off'.

IMPORTANT: cite panel numbers. Do not smooth over subtle face issues —
uncanny valley is exactly the class of defect humans notice but VLMs tend
to miss by saying "looks fine."

If the face is alive, expressive, and anatomically stable, verdict 'great'.
""".strip()
    agent_id = f"face_uncanny::{character_name}"
    title = f"Face & Uncanny Valley — {character_name}"
    r = _run(
        agent_id,
        title,
        prompt,
        video_bytes,
        image_bytes=face_grid,
        image_caption=f"FACE-CROP GRID — 16 panels of {character_name}'s face (panels 1-16):",
        ctx=ctx,
        role=ModelRole.CHARACTER_AGENT,
    )
    r.focus = f"Face & uncanny-valley for {character_name}"
    return r


# ─────────────────────────────────────────────────────────────────────────────
# Agent 3 — Motion & Weight
# ─────────────────────────────────────────────────────────────────────────────

def run_motion_weight(video_bytes: bytes, *, ctx: RunContext) -> AgentReport:
    prompt = """
You are a motion-quality reviewer. Your scope is HOW THINGS MOVE.

Flag only these observable defects:
- Floating (character hangs in the air without support)
- Foot-sliding (feet move without stepping)
- Phantom momentum (motion with no apparent cause)
- Impacts without reaction (landings with no squash / recoil)
- Bodies passing through solid objects

This is CGI animation — stylised bounce, exaggerated squash-and-stretch, and
cartoon physics are fine and should NOT be flagged. Only broken-looking,
non-stylised motion glitches count.

If motion looks intentional and coherent, verdict 'great'.
""".strip()
    return _run(
        "motion_weight", "Motion & Weight", prompt, video_bytes,
        ctx=ctx, role=ModelRole.SCENE_AGENT,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Agent 4 — Environment Stability
# ─────────────────────────────────────────────────────────────────────────────

def run_environment_stability(
    video_bytes: bytes, keyframe_grid: bytes, *, ctx: RunContext,
) -> AgentReport:
    prompt = """
You are a world-permanence reviewer. Your scope is the BACKGROUND ONLY —
ignore the characters entirely.

Use the 4×4 keyframe grid to compare background objects across panels.
Flag only:
- Static objects (signs, benches, buildings, trees, fences, vehicles) that
  TELEPORT, DISAPPEAR, DUPLICATE, or re-appear in a different world position
- Flickering or unstable background rendering
- Scene geometry that changes mid-clip

Camera-parallax motion of stable objects is NOT a defect — only changes in
their WORLD position count. Name each affected object and cite panel numbers.

If the background is stable, verdict 'great'.
""".strip()
    return _run(
        "environment_stability",
        "Environment Stability",
        prompt,
        video_bytes,
        image_bytes=keyframe_grid,
        image_caption="KEYFRAME GRID — 16 full-frame panels, evenly spaced, numbered 1-16:",
        ctx=ctx,
        role=ModelRole.SCENE_AGENT,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Agent 5 — Rendering Defects
# ─────────────────────────────────────────────────────────────────────────────

def run_rendering_defects(
    video_bytes: bytes, keyframe_grid: bytes, *, ctx: RunContext,
) -> AgentReport:
    prompt = """
You are a QC reviewer scanning for rendering artefacts. Your scope is
FRAME-LEVEL RENDERING QUALITY. Use the 4×4 keyframe grid to inspect
per-panel, then confirm with the video.

Flag only these pixel-level defects:
- Limb corruption (extra / missing / fused fingers, impossible joints)
- Hand rendering errors
- Morphing / warping geometry
- Textures swimming or crawling inconsistently
- Depth or occlusion errors (pieces passing through solid objects incorrectly)
- Single-frame flash artefacts

Describe each defect concretely and cite the panel number.

If rendering is clean across all panels, verdict 'great'.
""".strip()
    return _run(
        "rendering_defects",
        "Rendering Defects",
        prompt,
        video_bytes,
        image_bytes=keyframe_grid,
        image_caption="KEYFRAME GRID for rendering inspection (panels 1-16):",
        ctx=ctx,
        role=ModelRole.SCENE_AGENT,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Agent 6 — Audio Quality
# ─────────────────────────────────────────────────────────────────────────────

def run_audio_quality(video_bytes: bytes, *, ctx: RunContext) -> AgentReport:
    prompt = """
You are an audio-quality reviewer. You can hear the video's audio track.
Scope: TECHNICAL audio quality only — NOT whether the words match the script
(that's a different agent) and NOT whether mouths move (another agent).

Evaluate:
- Voice naturalness — robotic / stuttery / pitch-shifted?
- Clarity — intelligible or muffled / clipping / distorted?
- Age / character fit — voice matches character appearance?
- Artefacts — pops, dropouts, silences, mid-word cut-offs
- Background — ambient / SFX mix coherent?

Quote any glitched phrase verbatim.
If audio sounds clean and natural, verdict 'great'.
""".strip()
    return _run(
        "audio_quality", "Audio Quality", prompt, video_bytes,
        ctx=ctx, role=ModelRole.SCENE_AGENT,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Agent 7 — Lip-Sync Correspondence
# ─────────────────────────────────────────────────────────────────────────────

def run_lipsync_correspondence(
    video_bytes: bytes,
    speech_grid: Optional[bytes],
    speech_captions: list[str],
    *,
    ctx: RunContext,
) -> AgentReport:
    if not speech_grid:
        return AgentReport(
            agent_id="lipsync_correspondence",
            title="Lip-Sync Correspondence",
            focus="Does mouth movement accompany each spoken line?",
            verdict="great",
            summary="No detectable speech in the clip — lip-sync check is not applicable.",
            findings="",
            elapsed_s=0.0,
        )

    n_segs = len(speech_captions)
    segment_block = "\n".join(
        f"  Row {i + 1} (panels {2*i + 1} and {2*i + 2}): \"{c}\""
        for i, c in enumerate(speech_captions)
    )
    prompt = f"""
You are a lip-sync correspondence reviewer. Your scope is narrow and critical.

The grid has {n_segs} ROWS. Each row = ONE spoken line. LEFT panel is the
FIRST frame of the line; RIGHT panel is the LAST. For actual speech to be
happening, the character's mouth must change SHAPE between left and right
(forming different phonemes).

STEP 1 — WITHOUT READING THE CAPTIONS:
For each row, describe the MOUTH of the main visible character in the LEFT
panel, then the SAME character in the RIGHT panel. State plainly:
  - "IDENTICAL" (same pose — a smile or same open angle in both frames)
  - "DIFFERENT" (mouth is clearly forming a different shape)

CRITICAL DISTINCTION: a static cartoon smile (mouth slightly open, teeth
showing, unchanging) is NOT speaking. Speaking means mouth shape CHANGES
between start and end of the line.

STEP 2 — NOW READ THE CAPTIONS:
{segment_block}

STEP 3 — JUDGE:
- Every row IDENTICAL shapes → AUDIO FROM NOWHERE (major defect).
- Wrong speaker (wrong character's mouth moves) → major defect.
- Mouth changes but poorly matches phonemes → minor defect.
- Mouths clearly move in sync with speech → 'great'.

BIAS WARNING: audio plays regardless of whether a mouth moves. DO NOT
charitably assume a closed / smiling mouth is speaking because you hear
audio. Trust your eyes, not the audio.

Your `findings` MUST enumerate each row explicitly: "Row 1: Skye=CLOSED/OPEN,
Chase=CLOSED/OPEN; caption 'Yee-haw!'; assessment: ..."
""".strip()
    return _run(
        "lipsync_correspondence",
        "Lip-Sync Correspondence",
        prompt,
        video_bytes,
        image_bytes=speech_grid,
        image_caption="SPEECH-TIMESTAMP GRID (each row = one spoken line, LEFT=start frame, RIGHT=end frame):",
        ctx=ctx,
        role=ModelRole.SCENE_AGENT,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Agent 8 — Speech & Dialogue Coherence  (NEW)
# ─────────────────────────────────────────────────────────────────────────────

def run_speech_coherence(
    video_bytes: bytes,
    transcript: str,
    segments_block: str,
    *,
    ctx: RunContext,
) -> AgentReport:
    """
    Evaluate the LINGUISTIC quality of the spoken dialogue: are the words
    real, grammar sensible, ending complete, word-spacing natural?

    This task is primarily TEXT analysis of the Whisper transcript plus the
    per-segment timestamps (which let us detect long gaps or suspiciously
    fast pacing). It does NOT need Gemini Pro's full video-reasoning stack —
    running it on Flash cuts wall-clock from minutes to seconds. If the
    transcript already shows a dangling hyphen we flag mid-word cut-off
    from text alone; pauses are judged from segment timestamps.
    """
    import time as _time
    t0 = _time.time()

    if not transcript.strip():
        return AgentReport(
            agent_id="speech_coherence",
            title="Speech & Dialogue Coherence",
            focus="Linguistic quality of spoken dialogue",
            verdict="great",
            summary="No speech detected in the clip — speech coherence check skipped.",
            findings="",
            elapsed_s=round(_time.time() - t0, 1),
        )

    prompt = f"""
You are a speech-coherence reviewer. You are given the Whisper transcript of
an AI-generated short video, plus per-segment timestamps. Judge the LINGUISTIC
quality of the spoken content only. No video is provided (this is a
text-reasoning task).

Evaluate four dimensions:

A. REAL WORDS — Is every word in the transcript an actual word in the
   detected language? AI TTS sometimes produces plausible-sounding gibberish
   (e.g. "bluntrip", "folvery"). Quote the phrase if you find any.

B. GRAMMAR & SENSE — Are the sentences grammatically and semantically
   coherent? Short cartoon exclamations are fine; scrambled syntax is not.

C. MID-WORD CUT-OFF — Does the transcript end mid-word or mid-sentence?
   (e.g. "You're the b-", "This is a-"). A dangling hyphen or missing final
   punctuation signals a truncation defect.

D. WORD PACING — Do the SEGMENT TIMESTAMPS below suggest natural pacing?
   - Very fast: words fused / segments < 0.05s per word (machine-gun TTS).
   - Suspicious gaps: multi-second silence between consecutive segments
     mid-sentence is weird.

Quote exact phrases you critique. If the transcript is clean, verdict 'great'.

=== WHISPER TRANSCRIPT ===
{transcript}

=== SEGMENT TIMING ===
{segments_block}
""".strip()

    # Pure text reasoning — routed through TEXT_REASONING role (Flash in all
    # paid modes; mock returns a stub).
    parts = [prompt + "\n\n" + _OUTPUT_CONTRACT]
    last_exc = None
    data = None
    try:
        resp = call_model(
            ctx=ctx,
            role=ModelRole.TEXT_REASONING,
            agent_id="speech_coherence",
            contents=parts,
            config=types.GenerateContentConfig(
                temperature=0.1,
                response_mime_type="application/json",
                http_options=types.HttpOptions(timeout=60_000),
            ),
        )
        data = _parse_json((resp.text or "").strip())
    except Exception as exc:
        last_exc = exc

    if data is None:
        return AgentReport(
            agent_id="speech_coherence",
            title="Speech & Dialogue Coherence",
            focus="Linguistic quality of spoken dialogue",
            verdict="great",
            summary=f"(agent error: {last_exc})",
            findings=str(last_exc or "unparseable"),
            elapsed_s=round(_time.time() - t0, 1),
        )

    return AgentReport(
        agent_id="speech_coherence",
        title="Speech & Dialogue Coherence",
        focus="Linguistic quality of spoken dialogue",
        verdict=data["verdict"],
        summary=data["summary"],
        findings=data["findings"],
        elapsed_s=round(_time.time() - t0, 1),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Facial Topology Audit — geometric / structural integrity of facial features
#
# This agent does NOT look at expressions, identity, or "feels alive". It
# forces per-panel NUMERIC measurements (aspect ratio, shape descriptor,
# upper-eyelid arc) on a character's EYE-CROP grid, then detects structural
# morphs (e.g. an eye going round→almond over the clip) that the soft
# "face quality" agents miss. Inspired by the Morphological Inconsistency
# framing: compare the geometric skeleton, not the feeling.
# ─────────────────────────────────────────────────────────────────────────────

def run_facial_topology_audit(
    video_bytes: bytes,
    eye_grid: bytes,
    character_name: str = "the primary character",
    *,
    ctx: RunContext,
) -> AgentReport:
    import time as _time
    t0 = _time.time()

    topology_prompt = f"""
ROLE: You are a Facial Topology Analyst. Your scope is the geometric
structural integrity of **{character_name}**'s eyes across time. You are
NOT evaluating expression, mood, or animation quality. You are measuring
SHAPES.

You are given a grid of 24 panels, each a zoomed-in crop of
{character_name}'s eye region at a different moment in the clip.

STEP 1 — PER-PANEL NUMERIC MEASUREMENTS

For each panel, report:
  - aspect_ratio:  estimated WIDTH / HEIGHT of the visible eye opening.
                   Scale: round/wide-open ≈ 1.0, oval ≈ 1.5, almond ≈ 2.0+,
                   squinting ≈ 0.5, closed = 0.0. Use ONE eye (the more
                   clearly visible one) CONSISTENTLY across all panels.
  - shape:         one of {{"round", "oval", "almond", "angular", "closed", "unclear"}}
  - eyelid_arc:    shape of the upper eyelid line. One of
                   {{"smooth-convex", "flat", "angular", "inverted", "unclear"}}
  - view_angle:    approximate viewing angle of the face. One of
                   {{"frontal", "three_quarter", "profile", "above", "below", "unclear"}}

Report EVERY panel. Use "unclear" only for genuine occlusion or extreme
motion blur. Do not skip.

STEP 2 — AGGREGATE ANALYSIS

Compute:
  - aspect_ratio_range:  max − min of aspect_ratio across panels where
                          shape is neither "closed" nor "unclear".
  - distinct_shapes:     the set of shape descriptors used, excluding
                          "closed" (a blink) and "unclear".
  - structural_morphs:   list of specific panel pairs where the shape
                          changes between fundamentally different
                          descriptors (e.g. "round" → "almond") AND the
                          view_angle is similar (head-pose normalization
                          rule: only compare same-angle panels).

STEP 3 — VERDICT

A genuine MORPHOLOGICAL INCONSISTENCY requires:
  (a) Two or more panels at the SAME view_angle
  (b) Eye is open in both (not blinking / squinting)
  (c) The shape descriptor differs between them
      OR the aspect_ratio differs by more than 0.7

Blinks (closed → open) are NOT defects. Squinting / wide-eyed for
emotional beats is NOT a defect (those are expression changes).
What you flag is the underlying 3D-mesh shape CHANGING without cause.

Verdict rules:
  - "great": no morphological inconsistency found; eye shape is stable
             across same-angle open-eye panels.
  - "minor_issues": one suspicious transition at similar angles.
  - "major_issues": multiple structural morphs or a clear start→end drift
                    (e.g. Panel 1 = round, Panel 24 = almond, all frontal
                    open-eye).

RETURN EXACTLY THIS JSON SHAPE — nothing else:

{{
  "per_panel": [
    {{"panel": 1, "aspect_ratio": 1.0, "shape": "round",
      "eyelid_arc": "smooth-convex", "view_angle": "frontal"}},
    ... (24 entries total) ...
  ],
  "aspect_ratio_range": <float>,
  "distinct_shapes": ["round", ...],
  "structural_morphs": [
    {{"panel_a": 1, "panel_b": 18,
      "observation": "Panel 1 was round (ratio 0.95) at frontal angle;
                     panel 18 is almond (ratio 2.1) at frontal angle;
                     structural shape change with no blink in between."}}
  ],
  "verdict": "great" | "minor_issues" | "major_issues",
  "summary": "<one sentence>"
}}

Bias warning: this is NOT face recognition. You are not asked to confirm
identity. You ARE asked whether a 3D character's eye-hole GEOMETRY stays
structurally consistent. Treat the panels like engineering drawings.
""".strip()

    parts: list = [
        types.Part.from_bytes(data=video_bytes, mime_type="video/mp4"),
        f"EYE-CROP GRID for {character_name} — 24 panels, each zoomed to the eye region:",
        types.Part.from_bytes(data=eye_grid, mime_type="image/png"),
        topology_prompt,
    ]

    last_exc = None
    data = None
    try:
        resp = call_model(
            ctx=ctx,
            role=ModelRole.CHARACTER_AGENT,
            agent_id=f"facial_topology_audit::{character_name}",
            contents=parts,
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
                http_options=types.HttpOptions(timeout=240_000),
            ),
        )
        text = (resp.text or "").strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            text = m.group(0)
        data = json.loads(text)
    except Exception as exc:
        last_exc = exc

    if data is None:
        return AgentReport(
            agent_id=f"facial_topology_audit::{character_name}",
            title=f"Facial Topology Audit — {character_name}",
            focus=f"Geometric eye-shape integrity for {character_name}",
            verdict="great",
            summary=f"(agent error: {last_exc})",
            findings=str(last_exc or "unparseable"),
            elapsed_s=round(_time.time() - t0, 1),
        )

    verdict = str(data.get("verdict", "great")).lower().strip()
    if verdict not in {"great", "minor_issues", "major_issues"}:
        verdict = "great"

    # Build human-readable findings from the structured output
    lines = []
    try:
        ar_range = float(data.get("aspect_ratio_range", 0))
        lines.append(f"Aspect-ratio range (open-eye panels): {ar_range:.2f}")
    except (TypeError, ValueError):
        pass
    distinct = data.get("distinct_shapes", []) or []
    if distinct:
        lines.append(f"Distinct shape descriptors observed: {', '.join(distinct)}")
    morphs = data.get("structural_morphs", []) or []
    if morphs:
        lines.append("")
        lines.append("Structural morphs detected:")
        for m in morphs:
            obs = m.get("observation", "")
            pa = m.get("panel_a", "?"); pb = m.get("panel_b", "?")
            lines.append(f"  • Panel {pa} vs Panel {pb}: {obs}")
    else:
        lines.append("No structural morphs found at matching view-angles.")
    lines.append("")
    # Include first & last few per-panel rows as evidence
    per_panel = data.get("per_panel", []) or []
    if per_panel:
        lines.append("Per-panel measurements (first 6 and last 6):")
        for p in per_panel[:6] + per_panel[-6:]:
            lines.append(
                f"  Panel {p.get('panel', '?'):>3}: "
                f"ratio={p.get('aspect_ratio', '?')} · "
                f"shape={p.get('shape', '?')} · "
                f"arc={p.get('eyelid_arc', '?')} · "
                f"angle={p.get('view_angle', '?')}"
            )

    summary = str(data.get("summary", "")).strip() or f"Facial topology audit on {character_name}."

    return AgentReport(
        agent_id=f"facial_topology_audit::{character_name}",
        title=f"Facial Topology Audit — {character_name}",
        focus=f"Geometric eye-shape integrity for {character_name}",
        verdict=verdict,
        summary=summary,
        findings="\n".join(lines),
        elapsed_s=round(_time.time() - t0, 1),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Agent 9 — Character Consistency Audit (judge mode, strict rubric)
# ─────────────────────────────────────────────────────────────────────────────

def run_character_consistency_audit(
    video_bytes: bytes,
    face_grid: bytes,
    character_name: str = "the primary character",
    *,
    ctx: RunContext,
) -> AgentReport:
    """
    Strict Judge-mode audit of character drift across the face-crop grid.

    Uses a fixed four-axis rubric (Rendering / Geometry / Assets / Color) with
    numeric drift scores 0-10. The VLM compares every panel against Panel 1
    (reference) and reports the WORST drift observed across the grid. Output
    format is strict: four metric lines + a final conclusion.

    Verdict is derived from the aggregate score (sum of the four metrics,
    max 40): 0-8 great, 9-20 minor_issues, 21-40 major_issues.
    """
    import time
    import re as _re

    judge_prompt = f"""
Role: You are an automated Visual Consistency Judge. Your sole purpose is to
detect and quantify "Character Drift" between frames for **{character_name}**.
Do not offer creative advice or generation tips.

You are given a 4×4 FACE-CROP GRID of 16 panels — every panel should show the
same character, **{character_name}**, across the clip. Treat Panel 1 as the
REFERENCE. For each other panel, compare the face against the reference on
the four metrics below. Report the WORST drift observed across the grid.

If a panel clearly does NOT show {character_name} (crop failure, different
character), skip it and note it in findings.

Evaluation Metrics (the Delta):

Rendering Delta: Identify changes in shader complexity, lighting models
(e.g. flat to volumetric), and texture resolution.

Geometric Delta: Identify shifts in the underlying mesh, bone structure,
muzzle volume, and ear placement.

Asset Delta: Compare logos, badges, and clothing patterns for any change in
"vector" precision or 3D depth.

Color Delta: Detect shifts in hue, saturation, or luminance in the
character's primary palette.

For each metric, give a Drift Score 0-10 where 0 is identical and 10 is a
complete style collapse.

Your response must be a JSON object matching this shape exactly:
{{
  "metric_rendering": {{ "description": "<short description>", "score": <int 0-10> }},
  "metric_geometry":  {{ "description": "<short description>", "score": <int 0-10> }},
  "metric_assets":    {{ "description": "<short description>", "score": <int 0-10> }},
  "metric_color":     {{ "description": "<short description>", "score": <int 0-10> }},
  "pass_fail": "PASS" | "FAIL",
  "aggregate_score": <int 0-40>
}}

Use PASS when aggregate_score ≤ 8, FAIL otherwise.
Respond with JSON only, no prose.
""".strip()

    parts: list = [types.Part.from_bytes(data=video_bytes, mime_type="video/mp4")]
    parts.append("FACE-CROP GRID — 16 panels, Panel 1 is the REFERENCE for comparison:")
    parts.append(types.Part.from_bytes(data=face_grid, mime_type="image/png"))
    parts.append(judge_prompt)

    t0 = time.time()
    last_exc = None
    data = None
    try:
        resp = call_model(
            ctx=ctx,
            role=ModelRole.CHARACTER_AGENT,
            agent_id=f"character_consistency_audit::{character_name}",
            contents=parts,
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
                http_options=types.HttpOptions(timeout=240_000),
            ),
        )
        text = (resp.text or "").strip()
        m = _re.search(r"\{.*\}", text, _re.DOTALL)
        if m:
            text = m.group(0)
        data = json.loads(text)
    except Exception as exc:
        last_exc = exc

    if data is None:
        return AgentReport(
            agent_id=f"character_consistency_audit::{character_name}",
            title=f"Character Consistency Audit — {character_name}",
            focus=f"Strict four-axis drift rubric for {character_name}",
            verdict="great",
            summary=f"(agent error: {last_exc})",
            findings=str(last_exc or "unparseable"),
            elapsed_s=round(time.time() - t0, 1),
        )

    def _metric_line(label: str, key: str) -> str:
        m = data.get(key, {}) or {}
        desc = str(m.get("description", "")).strip()
        score = m.get("score", 0)
        try:
            score = int(score)
        except (TypeError, ValueError):
            score = 0
        score = max(0, min(10, score))
        return f"[{label}]: {desc} | Score: {score}/10"

    try:
        agg = int(data.get("aggregate_score", 0))
    except (TypeError, ValueError):
        agg = 0
    agg = max(0, min(40, agg))
    pass_fail = str(data.get("pass_fail", "FAIL")).upper().strip()
    if pass_fail not in {"PASS", "FAIL"}:
        pass_fail = "FAIL" if agg > 8 else "PASS"

    if agg <= 8:
        verdict = "great"
    elif agg <= 20:
        verdict = "minor_issues"
    else:
        verdict = "major_issues"

    findings_strict = "\n".join([
        "Metric 1 " + _metric_line("Rendering", "metric_rendering"),
        "Metric 2 " + _metric_line("Geometry",  "metric_geometry"),
        "Metric 3 " + _metric_line("Assets",    "metric_assets"),
        "Metric 4 " + _metric_line("Color",     "metric_color"),
        "",
        f"Final Conclusion: {pass_fail} | Aggregate Drift Score: {agg}/40",
    ])

    summary = f"{character_name}: aggregate drift {agg}/40 — {pass_fail}"
    return AgentReport(
        agent_id=f"character_consistency_audit::{character_name}",
        title=f"Character Consistency Audit — {character_name}",
        focus=f"Strict four-axis drift rubric for {character_name}",
        verdict=verdict,
        summary=summary,
        findings=findings_strict,
        elapsed_s=round(time.time() - t0, 1),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Agent 10 — Prompt Fidelity
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Blind Captioner — describes the video WITHOUT knowing the prompt.
# Its output feeds the Comparator (and the Prompt Reasoner).
# ─────────────────────────────────────────────────────────────────────────────

def run_blind_captioner(video_bytes: bytes, *, ctx: RunContext) -> AgentReport:
    """
    Generate a purpose-blind, granular, multi-temporal description of the
    video. The VLM does NOT see the generation prompt, so it cannot be
    primed to describe prompt elements into existence. It describes strictly
    what it observes.

    This report is consumed by `run_prompt_vs_caption` (replacing the
    prompt-primed Prompt Fidelity agent) and by the Prompt Reasoner.
    """
    prompt = """
You are describing this video for a visually-impaired viewer. You do NOT
know the intent behind the video. Stay strictly tethered to visual and
audible evidence.

Describe the clip in THREE temporal slices:

A. START (first ~15% of the clip) — the initial static state: setting,
   characters visible, their posture and gaze, hand/foot placement, any
   objects they hold, the camera framing. Body proportions, colors, visible
   accessories. Mood implied by expression and body language.

B. MIDDLE (central ~50%) — what changes between start and end. Each
   character's body motion, gaze shifts, facial expression changes, hand
   movements, object interactions. Camera motion. Background motion.
   Any DIALOGUE HEARD — quote it verbatim as you hear it. Note which
   character's mouth is visibly moving.

C. END (last ~15%) — the final state. Consistency with START: same
   character design? same clothing? same accessories? same scene
   geometry?

Structure your answer in `findings` as three paragraphs labelled A, B, C.
In `summary`, give one sentence describing what the video depicts overall.
`verdict` should always be "great" — this agent does not judge quality,
only describes.

CRITICAL: describe what you ACTUALLY see and hear, with granularity. Do
not editorialise. Do not reference anything labelled "the prompt" —
you have not been given a prompt. If something is ambiguous, say so
("the character appears to be holding something, possibly a toy").
""".strip()
    return _run(
        "blind_captioner", "Blind Captioner", prompt, video_bytes,
        ctx=ctx, role=ModelRole.SCENE_AGENT,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Prompt-vs-Caption Comparator — deduces discrepancies between the intended
# prompt and the blind caption. Receives NO video — pure text reasoning.
# This replaces the old (prompt-primed) Prompt Fidelity agent.
# ─────────────────────────────────────────────────────────────────────────────

def run_prompt_vs_caption(
    blind_caption: str,
    transcript: str,
    prompt_text: str,
    *,
    ctx: RunContext,
) -> AgentReport:
    system = f"""
You are a deduction engine comparing intent vs observation. You are given:

  (1) the ORIGINAL GENERATION PROMPT (intended output),
  (2) a BLIND VIDEO DESCRIPTION written by an observer who had never seen
      the prompt (objective observation of the video),
  (3) the WHISPER TRANSCRIPT of the audio.

You have NO access to the video itself. Your job is PURE TEXT REASONING:
find logical contradictions or unfulfilled promises between (1) and (2)/(3).

Method:

1. List every concrete, named ACTION, OBJECT, ATTRIBUTE, or EVENT in the
   prompt. (e.g. "the pups turn their heads left, then right"; "the pink
   pup is doing a backflip"; "a tall tower on a green hill"; "dialogue
   assigned to the blue pup".)

2. For each one, find it (or its absence) in the blind description.
   Classify as:
      FULFILLED   — the description confirms it happened / is present
      MISSING     — the description does not mention it (defect)
      CONTRADICTED — the description positively describes something
                     different (defect)
      PARTIAL     — partially present
      UNKNOWN     — insufficient evidence in the description

3. Also flag any ADDED elements — things in the blind description that the
   prompt did not ask for and that might indicate unintended model bias.

In `findings`, produce a bullet list: `[STATUS] <prompt element> — <quote
from blind description or "not mentioned">`. Prioritise MISSING and
CONTRADICTED items. In `summary`, give a one-sentence judgment.

Verdict:
  great         — every substantive prompt element is FULFILLED
  minor_issues  — 1–2 MISSING or PARTIAL elements, none central
  major_issues  — any CONTRADICTED element OR a MISSING central action OR
                  wrong speaker assignment

BIAS WARNING: the blind description is tethered to visual reality; the
prompt is intent. Trust the description. Do NOT assume the prompt
happened unless the description supports it.

DO NOT do pedantic string-matching. Tiny wording differences are fine.
Meaning and observed reality are what matter.

=== ORIGINAL GENERATION PROMPT ===
{prompt_text}

=== BLIND VIDEO DESCRIPTION ===
{blind_caption}

=== WHISPER TRANSCRIPT ===
{transcript}
""".strip()

    # Pure text reasoning — routed through TEXT_REASONING role.
    import time as _time
    t0 = _time.time()
    last_exc = None
    data = None
    try:
        resp = call_model(
            ctx=ctx,
            role=ModelRole.TEXT_REASONING,
            agent_id="prompt_vs_caption",
            contents=[system + "\n\n" + _OUTPUT_CONTRACT],
            config=types.GenerateContentConfig(
                temperature=0.1,
                response_mime_type="application/json",
                http_options=types.HttpOptions(timeout=120_000),
            ),
        )
        data = _parse_json((resp.text or "").strip())
    except Exception as exc:
        last_exc = exc

    if data is None:
        return AgentReport(
            agent_id="prompt_vs_caption",
            title="Prompt vs Blind Caption",
            focus="Deduction-based prompt fidelity",
            verdict="great",
            summary=f"(agent error: {last_exc})",
            findings=str(last_exc or "unparseable"),
            elapsed_s=round(_time.time() - t0, 1),
        )

    return AgentReport(
        agent_id="prompt_vs_caption",
        title="Prompt vs Blind Caption",
        focus="Deduction-based prompt fidelity",
        verdict=data["verdict"],
        summary=data["summary"],
        findings=data["findings"],
        elapsed_s=round(_time.time() - t0, 1),
    )


def run_prompt_fidelity(
    video_bytes: bytes, transcript: str, prompt_text: str, *, ctx: RunContext,
) -> AgentReport:
    prompt = """
You are the script supervisor. You have the intended GENERATION PROMPT and
the Whisper TRANSCRIPT. Scope: does the video deliver the prompt?

A. SCENE FIDELITY — Setting, characters, overall action shown match the prompt?

B. ACTION vs DIALOGUE — If dialogue references an action ("look at my backflip"),
   does that action actually happen?

C. SPEAKER ASSIGNMENT — If the prompt scripts character A to say a line, is
   character A the one whose mouth moves during it?

Quote dialogue verbatim; describe specific visual evidence.

DO NOT compare the transcript word-by-word to the prompt — tiny ASR errors
like "yee-ho" vs "yee-haw" are not defects. Care about MEANING.

Wrong speaker or promised-action missing is MAJOR.
If everything lines up, verdict 'great'.
""".strip()
    extra = f"WHISPER TRANSCRIPT:\n{transcript}\n\nORIGINAL GENERATION PROMPT:\n{prompt_text}"
    return _run(
        "prompt_fidelity", "Prompt Fidelity", prompt, video_bytes, extra,
        ctx=ctx, role=ModelRole.SCENE_AGENT,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Prompt Reasoner (Deduction Engine) — optional downstream agent
# ─────────────────────────────────────────────────────────────────────────────

def run_prompt_reasoner(
    video_bytes: bytes,
    reports: list[AgentReport],
    aggregator: dict,
    prompt_text: str,
    *,
    ctx: RunContext,
) -> dict:
    """
    The "Visual Detective" / Deduction Engine.

    Downstream of the specialist agents and aggregator. Given the original
    prompt, the specialist findings, and the video itself, propose a
    rewritten prompt that removes the SEMANTIC TRIGGERS causing the
    model's observed failures.

    Guiding principle: the model is a next-frame predictor pulled around
    by its training biases. The fix is never "tell the model not to do X"
    — it is to add a new physical anchor that makes X impossible.

    Returns a dict (never raises). If the aggregator verdict is PASS with
    no defects worth addressing, the agent returns a skip marker instead
    of a rewrite.
    """
    if not reports and not aggregator:
        return {
            "skipped_reason": "no specialist reports available",
            "diagnosed_conflicts": [],
            "trigger_attributions": [],
            "counterfactual_edits": [],
            "rewritten_prompt": "",
            "rationale": "",
            "elapsed_s": 0.0,
        }

    try:
        score = int(aggregator.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    overall = str(aggregator.get("overall_verdict", "")).upper()
    has_real_defects = any(
        r.verdict in {"minor_issues", "major_issues"} for r in reports
    )
    if overall == "PASS" and score >= 90 and not has_real_defects:
        return {
            "skipped_reason": "video passed cleanly — no prompt rewrite warranted",
            "diagnosed_conflicts": [],
            "trigger_attributions": [],
            "counterfactual_edits": [],
            "rewritten_prompt": prompt_text,
            "rationale": "",
            "elapsed_s": 0.0,
        }

    specialist_block = "\n\n".join(
        f"### {r.title} — [{r.verdict}]\n"
        f"Summary: {r.summary}\n"
        f"Findings: {r.findings}"
        for r in reports
    )
    agg_block = "\n".join([
        f"Verdict: {aggregator.get('overall_verdict', '')}",
        f"Score: {aggregator.get('score', '')}",
        f"Headline: {aggregator.get('headline', '')}",
        f"What works: {aggregator.get('what_works', '')}",
        f"What breaks: {aggregator.get('what_breaks', '')}",
        f"Top issue: {aggregator.get('top_issue', '')}",
    ])

    system = f"""
You are the PROMPT REASONING AGENT — a "visual detective" whose job is to
understand WHY a text-to-video model failed to deliver the requested video
and propose a SMARTER prompt that removes the hidden triggers behind the
failure.

═══════════════════════════════════════════════════════════════════════════
CORE PHILOSOPHY
═══════════════════════════════════════════════════════════════════════════

Video models are next-frame prediction engines. They do not "read" prompts
— they pattern-match words to visual automations learned from training
data. If a phrase is statistically linked to a motion, a lighting change,
or a composition, the model will produce that automation regardless of
what the rest of the prompt says.

Negative prompts ("the car is NOT moving") are mostly ignored because the
token "moving" still activates motion priors. The correct intervention is
TRIGGER REMOVAL: rewrite the prompt so the physical setup makes the
unwanted behavior logically impossible.

Examples of the pattern:
  • Prompt says "stationary car" but wheels spin + background scrolls.
    Hidden trigger: "hands on the steering wheel" → driving.
    Smart fix: "driver turned sideways to the passenger, hands on knees."
  • Prompt says "silent cliff-top portrait" but hair whips violently.
    Hidden trigger: "cliff" → high winds in training data.
    Smart fix: "hair tied in a tight bun" — removes the wind's canvas.
  • Prompt says "quiet cafe conversation" but characters gesture wildly.
    Hidden trigger: "busy cafe" → commotion as a theme.
    Smart fix: "holding a warm tea cup with both hands" — locks the torso.
  • Prompt says "calm night" but face flickers in changing light.
    Hidden trigger: "TV" or "neon" → attempted dynamic ray-tracing.
    Smart fix: "room lit by a static, warm lamp."

The word "static" applied to lighting, the word "tied" applied to hair,
the word "resting" applied to hands — these are ANCHORS. An anchor is a
physical fact that the model must honor, and that precludes the error.

═══════════════════════════════════════════════════════════════════════════
SCOPE
═══════════════════════════════════════════════════════════════════════════

You fix defects that are plausibly caused by PROMPT-ACTIVATED TRAINING BIAS:
  • Unintended motion (things moving that shouldn't)
  • Hyperactive / unstable composition
  • Lighting or reflection instability
  • Environment flicker from crowded scene descriptions
  • Prompt-vs-outcome mismatches where the outcome is a known bias
    ("cliff" → wind, "wet" → reflections, "busy" → commotion)

You DO NOT attempt to fix defects caused by model capacity limits:
  • Character identity drift / face morphing
  • Anatomy corruption (extra fingers, melting limbs)
  • Lip-sync failures
  • Audio artefacts / gibberish speech
  • Text rendering
If the defect class is capacity-limited, flag it and DO NOT pretend a
prompt rewrite will help — that would mislead the user.

═══════════════════════════════════════════════════════════════════════════
REASONING PROTOCOL — follow all four steps
═══════════════════════════════════════════════════════════════════════════

STEP 1 · GAP ANALYSIS
  Compare three vectors:
    (a) Intent — what the original prompt asked for.
    (b) Outcome — what the specialists observed in the video.
    (c) Bias — which training-data association most likely bridges (a)→(b).
  Only produce conflicts that are TRIGGER-BASED. Skip capacity failures.

STEP 2 · INFLUENTIAL OBJECT MAPPING
  Examine the video directly. For each conflict, identify the concrete
  visible element(s) or phrase(s) in the prompt that most plausibly caused
  the model to activate the unwanted automation. Be specific — "hands on
  the wheel", not "driving-related content".

STEP 3 · COUNTERFACTUAL SIMULATION (mental)
  For each candidate edit, ask: "If I remove or replace this element, does
  the probability of the unwanted automation drop?" Prefer edits that
  introduce a POSITIVE physical anchor (a new, concrete, static fact) over
  edits that just delete words. Avoid negations.

STEP 4 · SUBTLE REDESIGN
  Produce the full rewritten prompt. Constraints:
    • Preserve the creative intent — same scene, same characters, same
      emotional tone. This is minimum-edit, not a new concept.
    • Use concrete, physical language. Prefer "hands resting on knees"
      over "hands are still".
    • Never use "not", "no", "without", "never" to express an absence of
      motion or effect. Always express it as a physical fact that makes
      the absence automatic.
    • Keep the prompt roughly the same length as the original.

═══════════════════════════════════════════════════════════════════════════
INPUTS
═══════════════════════════════════════════════════════════════════════════

ORIGINAL GENERATION PROMPT:
{prompt_text}

AGGREGATOR VERDICT:
{agg_block}

SPECIALIST REPORTS:
{specialist_block}

You also have the full native video.

═══════════════════════════════════════════════════════════════════════════
OUTPUT — JSON only, matching this schema exactly
═══════════════════════════════════════════════════════════════════════════

{{
  "skipped_reason": null,
  "diagnosed_conflicts": [
    {{
      "intent": "<what the prompt asked for, concretely>",
      "outcome": "<what the video delivered, concretely — grounded in a specialist finding>",
      "source_agent": "<agent_id that surfaced this, e.g. motion_weight>",
      "is_capacity_limited": false
    }}
  ],
  "trigger_attributions": [
    {{
      "visible_element": "<concrete element observed in the video, e.g. 'driver's hands gripping the steering wheel'>",
      "prompt_phrase": "<exact substring from the original prompt that likely activated this, or '' if it is an implicit genre bias>",
      "hypothesized_bias": "<the training-data association, e.g. 'steering-wheel grip → driving motion'>",
      "confidence": "high"
    }}
  ],
  "counterfactual_edits": [
    {{
      "operation": "replace",
      "from": "<exact substring from original prompt>",
      "to": "<the new physical anchor>",
      "reasoning": "<one sentence: why this severs the trigger>"
    }}
  ],
  "rewritten_prompt": "<the full revised prompt, edits applied, same creative intent>",
  "rationale": "<one short paragraph: the single strongest anchor added and the bias it defeats>"
}}

If every defect is capacity-limited and no trigger-based fix applies, set
"skipped_reason" to a short explanation, leave edits empty, and return the
original prompt unchanged as "rewritten_prompt".

Confidence values allowed: "high" | "medium" | "low".
Operation values allowed: "replace" | "insert" | "delete".
Respond with JSON only. No prose before or after.
""".strip()

    parts: list = [types.Part.from_bytes(data=video_bytes, mime_type="video/mp4"), system]

    t0 = time.time()
    last_exc = None
    data = None
    try:
        resp = call_model(
            ctx=ctx,
            role=ModelRole.PROMPT_REASONER,
            agent_id="prompt_reasoner",
            contents=parts,
            config=types.GenerateContentConfig(
                temperature=0.2,
                response_mime_type="application/json",
                http_options=types.HttpOptions(timeout=240_000),
            ),
        )
        text = (resp.text or "").strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            text = m.group(0)
        data = json.loads(text)
    except Exception as exc:
        last_exc = exc

    elapsed = round(time.time() - t0, 1)

    if data is None:
        return {
            "skipped_reason": f"(agent error: {last_exc})",
            "diagnosed_conflicts": [],
            "trigger_attributions": [],
            "counterfactual_edits": [],
            "rewritten_prompt": prompt_text,
            "rationale": "",
            "elapsed_s": elapsed,
        }

    def _as_list(v) -> list:
        return v if isinstance(v, list) else []

    def _as_str(v) -> str:
        return str(v).strip() if v is not None else ""

    conflicts = []
    for c in _as_list(data.get("diagnosed_conflicts")):
        if not isinstance(c, dict):
            continue
        conflicts.append({
            "intent": _as_str(c.get("intent")),
            "outcome": _as_str(c.get("outcome")),
            "source_agent": _as_str(c.get("source_agent")),
            "is_capacity_limited": bool(c.get("is_capacity_limited", False)),
        })

    triggers = []
    for t in _as_list(data.get("trigger_attributions")):
        if not isinstance(t, dict):
            continue
        conf = _as_str(t.get("confidence")).lower() or "medium"
        if conf not in {"high", "medium", "low"}:
            conf = "medium"
        triggers.append({
            "visible_element": _as_str(t.get("visible_element")),
            "prompt_phrase": _as_str(t.get("prompt_phrase")),
            "hypothesized_bias": _as_str(t.get("hypothesized_bias")),
            "confidence": conf,
        })

    edits = []
    for e in _as_list(data.get("counterfactual_edits")):
        if not isinstance(e, dict):
            continue
        op = _as_str(e.get("operation")).lower() or "replace"
        if op not in {"replace", "insert", "delete"}:
            op = "replace"
        edits.append({
            "operation": op,
            "from": _as_str(e.get("from")),
            "to": _as_str(e.get("to")),
            "reasoning": _as_str(e.get("reasoning")),
        })

    rewritten = _as_str(data.get("rewritten_prompt")) or prompt_text
    rationale = _as_str(data.get("rationale"))
    skipped = data.get("skipped_reason")
    skipped = _as_str(skipped) if skipped else None

    return {
        "skipped_reason": skipped,
        "diagnosed_conflicts": conflicts,
        "trigger_attributions": triggers,
        "counterfactual_edits": edits,
        "rewritten_prompt": rewritten,
        "rationale": rationale,
        "elapsed_s": elapsed,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Aggregator
# ─────────────────────────────────────────────────────────────────────────────

def run_aggregator(
    reports: list[AgentReport], prompt_text: str, *, ctx: RunContext,
) -> dict:
    agent_blocks = "\n\n".join(
        f"### {r.title} — [{r.verdict}]\n{r.summary}\n\nDetails: {r.findings}"
        for r in reports
    )

    system = f"""
You are the supervising QA director. Nine specialist agents have each watched
the video and written their analyses. Synthesise into a final verdict.

--- ORIGINAL GENERATION PROMPT ---
{prompt_text}

--- SPECIALIST REPORTS ---
{agent_blocks}

Respond as JSON with exactly these keys:
{{
  "overall_verdict": "PASS" | "CONDITIONAL_PASS" | "FAIL",
  "score": <integer 0-100>,
  "headline": "<one sentence a producer would read>",
  "what_works": "<one paragraph, grounded in the reports>",
  "what_breaks": "<one paragraph, grounded in the reports — real failures only>",
  "top_issue": "<the single most important issue to fix if only one could be>"
}}

Scoring guide:
- 85-100 PASS: deliverable, polish notes only.
- 70-84 CONDITIONAL_PASS: watchable but clear flaws.
- 40-69 FAIL: significant defects — regenerate.
- 0-39 FAIL: fundamentally broken.

Weighting:
- Prompt Fidelity + Lip-Sync + Speech Coherence defects are MOST serious —
  a major issue on any alone drops to CONDITIONAL_PASS or FAIL.
- Face & Uncanny Valley issues are serious (viewers notice uncanny faces).
- Rendering defects are serious.
- Body Consistency issues are serious.
- Audio Quality and Environment Stability are moderate if isolated.
- Motion is moderate unless clearly broken.

Do NOT introduce claims not present in the reports. Trust the specialists.
Respond with JSON only.
""".strip()

    resp = call_model(
        ctx=ctx,
        role=ModelRole.AGGREGATOR,
        agent_id="aggregator",
        contents=system,
        config=types.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json",
            http_options=types.HttpOptions(timeout=240_000),
        ),
    )
    text = (resp.text or "").strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        text = m.group(0)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        obj = {"overall_verdict": "FAIL", "score": 0,
               "headline": "(aggregator unparseable)", "what_works": "",
               "what_breaks": text[:500], "top_issue": ""}
    verdict = str(obj.get("overall_verdict", "FAIL")).upper().replace(" ", "_")
    if verdict not in {"PASS", "CONDITIONAL_PASS", "FAIL"}:
        verdict = "FAIL"
    try:
        score = int(obj.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    return {
        "overall_verdict": verdict,
        "score": max(0, min(100, score)),
        "headline": str(obj.get("headline", "")).strip(),
        "what_works": str(obj.get("what_works", "")).strip(),
        "what_breaks": str(obj.get("what_breaks", "")).strip(),
        "top_issue": str(obj.get("top_issue", "")).strip(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Comparator — takes two completed report.json blobs and returns a verdict
# of which video is better. Pure text reasoning (no video, no images): the
# specialist work has already been done; we are reading the reports.
# ─────────────────────────────────────────────────────────────────────────────

def _summarise_report_for_comparator(report: dict, label: str) -> str:
    """Render a report.json into a compact text block suitable for the model."""
    agg = report.get("aggregator", {}) or {}
    parts = [
        f"=== VIDEO {label} ===",
        f"File: {report.get('video_file', '?')}",
        f"Aggregator verdict: {agg.get('overall_verdict', '?')}  "
        f"score={agg.get('score', '?')}/100",
        f"Headline: {agg.get('headline', '')}",
        f"What works: {agg.get('what_works', '')}",
        f"What breaks: {agg.get('what_breaks', '')}",
        f"Top issue: {agg.get('top_issue', '')}",
        "",
        "Per-agent reports:",
    ]
    for r in report.get("agents", []) or []:
        parts.append(
            f"  • [{r.get('verdict', '?')}] {r.get('title', '?')} — "
            f"{r.get('summary', '')}"
        )
    transcript = (report.get("transcript") or "").strip()
    if transcript:
        parts.append("")
        parts.append(f"Transcript: {transcript[:600]}")
    return "\n".join(parts)


def run_comparator(
    report_a: dict,
    report_b: dict,
    *,
    ctx: RunContext,
    label_a: str = "A",
    label_b: str = "B",
) -> dict:
    """Compare two completed reports and return a structured verdict.

    Returns a dict:
      {
        "winner": "A" | "B" | "tie",
        "confidence": "high" | "medium" | "low",
        "headline": "<one sentence>",
        "reasoning": "<one paragraph>",
        "per_axis": {
          "prompt_fidelity": "A" | "B" | "tie",
          "rendering": "A" | "B" | "tie",
          ...
        }
      }
    """
    block_a = _summarise_report_for_comparator(report_a, label_a)
    block_b = _summarise_report_for_comparator(report_b, label_b)

    system = f"""
You are an automated comparator. Two AI-generated videos have already been
analysed by a battery of specialist agents. Your job is to read the two
specialist reports below and decide which video is better OVERALL, and which
is better on each evaluation axis.

You do NOT have access to the videos themselves. Trust the specialists.

Comparison axes (be specific where the reports give signal):
  - prompt_fidelity   (does the video deliver what the prompt asked for?)
  - rendering         (limb / texture / hand artefacts)
  - face_uncanny      (face quality, expressions, uncanny smell)
  - character_consistency  (drift across the clip)
  - motion            (motion naturalness, foot-sliding, etc.)
  - environment       (background stability)
  - audio             (audio quality, voice clarity)
  - lipsync           (mouth-to-speech alignment)

Rules:
  - For each axis, choose the BETTER video, or "tie" only when reports are
    genuinely indistinguishable on that axis.
  - The OVERALL winner should reflect axis-weighting: prompt fidelity,
    lipsync, and rendering matter more than environment or motion when
    everything else is equal.
  - "confidence" reflects how clearly the reports separate the two videos.
    Use "high" only when one report has a clearly better verdict on
    multiple high-weight axes.

{block_a}

{block_b}

Respond with JSON only, matching this schema exactly:
{{
  "winner": "{label_a}" | "{label_b}" | "tie",
  "confidence": "high" | "medium" | "low",
  "headline": "<one sentence>",
  "reasoning": "<one paragraph grounded in the specialists' findings>",
  "per_axis": {{
    "prompt_fidelity": "{label_a}" | "{label_b}" | "tie",
    "rendering": "{label_a}" | "{label_b}" | "tie",
    "face_uncanny": "{label_a}" | "{label_b}" | "tie",
    "character_consistency": "{label_a}" | "{label_b}" | "tie",
    "motion": "{label_a}" | "{label_b}" | "tie",
    "environment": "{label_a}" | "{label_b}" | "tie",
    "audio": "{label_a}" | "{label_b}" | "tie",
    "lipsync": "{label_a}" | "{label_b}" | "tie"
  }}
}}
""".strip()

    resp = call_model(
        ctx=ctx,
        role=ModelRole.COMPARATOR,
        agent_id="comparator",
        contents=system,
        config=types.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json",
            http_options=types.HttpOptions(timeout=180_000),
        ),
    )
    text = (resp.text or "").strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        text = m.group(0)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        obj = {}

    valid_winners = {label_a, label_b, "tie"}

    def _coerce_winner(v) -> str:
        s = str(v or "").strip()
        return s if s in valid_winners else "tie"

    confidence = str(obj.get("confidence", "low")).lower().strip()
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"

    per_axis_in = obj.get("per_axis", {}) or {}
    per_axis: dict = {}
    for axis in (
        "prompt_fidelity", "rendering", "face_uncanny",
        "character_consistency", "motion", "environment",
        "audio", "lipsync",
    ):
        per_axis[axis] = _coerce_winner(per_axis_in.get(axis))

    return {
        "winner": _coerce_winner(obj.get("winner")),
        "confidence": confidence,
        "headline": str(obj.get("headline", "")).strip(),
        "reasoning": str(obj.get("reasoning", "")).strip(),
        "per_axis": per_axis,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Agent registry — split into scene-level (one-shot per video) and
# per-character (one-shot per detected character).
# Each entry: (agent_id, runner_fn, tuple-of-extra-kwargs)
# ─────────────────────────────────────────────────────────────────────────────

# Per-character agents — orchestrator calls these once per unique character
# detected in the clip. kwargs come from per-character grid builders.
PER_CHARACTER_AGENTS = [
    ("body_consistency",            run_body_consistency,            ("body_grid", "character_name")),
    ("face_uncanny",                run_face_uncanny,                ("face_grid", "character_name")),
    ("character_consistency_audit", run_character_consistency_audit, ("face_grid", "character_name")),
    # Geometric / structural audit on the eye region. Catches morphological
    # defects (e.g. eyes going round→almond mid-clip) that the soft
    # "face quality" agents dismiss with "looks fine".
    ("facial_topology_audit",       run_facial_topology_audit,       ("eye_grid",  "character_name")),
]

# Scene-level agents — one call per video regardless of character count.
#
# CHANGES THIS ITERATION:
# - VLM `lipsync_correspondence` replaced by classical optical-flow lipsync
#   (see `lipsync_flow.py`) — it was hallucinating.
# - `prompt_fidelity` kept as-is (primed baseline).
# - `blind_captioner` ADDED: describes the video without seeing the prompt.
# - `prompt_vs_caption` ADDED: deduction-only compare of prompt to blind
#   caption, no video. Runs AFTER blind_captioner completes.
# The two new agents sit alongside the existing prompt_fidelity so primed-vs-
# blind methodologies can be compared head-to-head.
SCENE_AGENTS = [
    ("motion_weight",         run_motion_weight,         ()),
    ("environment_stability", run_environment_stability, ("keyframe_grid",)),
    ("rendering_defects",     run_rendering_defects,     ("keyframe_grid",)),
    ("audio_quality",         run_audio_quality,         ()),
    ("speech_coherence",      run_speech_coherence,      ("transcript", "segments_block")),
    ("blind_captioner",       run_blind_captioner,       ()),
    ("prompt_fidelity",       run_prompt_fidelity,       ("transcript", "prompt_text")),
]

# Backward compatibility: keep a flat AGENTS list pointing at scene-only agents
# for any callers still using it. Orchestrator should use the split lists.
AGENTS = SCENE_AGENTS
