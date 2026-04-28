"""
Character detection via Gemini 2.5 Flash.

Replaces the old warm-fur HSV heuristic. For each frame we ask Gemini Flash
to return a JSON list of characters with their body and face bounding boxes.
Flash is fast and cheap (~$0.001 per call); calling it once per sampled frame
costs a few cents per video and gives us robust detection across arbitrary
character designs (dogs, humans, robots, anything).

Bounding box convention from Gemini:
  Returned as [ymin, xmin, ymax, xmax] in 0-1000 normalised coords.
  We convert to (x1, y1, x2, y2) in pixels.
"""
from __future__ import annotations

import concurrent.futures as cf
import io
import json
import re
from dataclasses import dataclass, asdict
from typing import List, Optional, Tuple

from google.genai import types
from loguru import logger
from PIL import Image

from .core import ModelRole, RunContext, call_model


@dataclass
class CharacterBbox:
    name: str                        # human-readable label, e.g. "pink dog", "robot"
    body: Tuple[int, int, int, int]  # (x1, y1, x2, y2) in pixels
    face: Optional[Tuple[int, int, int, int]] = None

    def to_dict(self) -> dict:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# Per-frame detection
# ─────────────────────────────────────────────────────────────────────────────

_DETECTION_PROMPT = """
Identify EVERY distinct character visible in this image. For each character
return both:
  - "body" bounding box covering the whole visible character (full body if
    visible, otherwise the largest visible portion — head+torso, etc.)
  - "face" bounding box covering just the face (eyes + mouth region). If the
    character is facing away or face is not visible, set face to null.

Respond with ONLY a JSON array, one object per character, no prose:
[
  {
    "name": "<short description e.g. 'pink dog', 'blue police dog', 'human girl'>",
    "body": [ymin, xmin, ymax, xmax],
    "face": [ymin, xmin, ymax, xmax] | null
  }
]

Bounding boxes are in normalised 0-1000 coordinates [ymin, xmin, ymax, xmax].
Return an empty array [] if no characters are visible.
""".strip()


def _parse_bboxes(text: str, img_w: int, img_h: int) -> List[CharacterBbox]:
    if not text:
        return []
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        text = m.group(0)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []

    def _to_pixels(bb) -> Optional[Tuple[int, int, int, int]]:
        if bb is None or not isinstance(bb, list) or len(bb) != 4:
            return None
        try:
            ymin, xmin, ymax, xmax = [float(v) for v in bb]
        except (TypeError, ValueError):
            return None
        x1 = int(max(0, min(1000, xmin)) * img_w / 1000)
        y1 = int(max(0, min(1000, ymin)) * img_h / 1000)
        x2 = int(max(0, min(1000, xmax)) * img_w / 1000)
        y2 = int(max(0, min(1000, ymax)) * img_h / 1000)
        if x2 - x1 < 5 or y2 - y1 < 5:
            return None
        return (x1, y1, x2, y2)

    out: List[CharacterBbox] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        body = _to_pixels(item.get("body"))
        if body is None:
            continue
        face = _to_pixels(item.get("face"))
        out.append(CharacterBbox(
            name=str(item.get("name") or "character"),
            body=body,
            face=face,
        ))
    return out


_DETECT_MAX_WIDTH = 512  # downscale before sending — normalised bboxes are resolution-agnostic


def detect_in_frame(frame_pil: Image.Image, *, ctx: RunContext) -> List[CharacterBbox]:
    """Detect characters in a single frame. Returns possibly-empty list.

    In mock mode this returns an empty list without touching the network —
    downstream code already handles the no-character fallback.
    """
    if ctx.is_mock:
        return []

    orig_w, orig_h = frame_pil.size

    # Resize for transport — Flash latency is dominated by input size
    if orig_w > _DETECT_MAX_WIDTH:
        ratio = _DETECT_MAX_WIDTH / orig_w
        small = frame_pil.resize(
            (_DETECT_MAX_WIDTH, int(orig_h * ratio)),
            Image.LANCZOS,
        )
    else:
        small = frame_pil

    buf = io.BytesIO()
    small.save(buf, format="JPEG", quality=80)
    img_bytes = buf.getvalue()
    try:
        resp = call_model(
            ctx=ctx,
            role=ModelRole.DETECTION,
            agent_id="detection::frame",
            contents=[
                types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
                _DETECTION_PROMPT,
            ],
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
                http_options=types.HttpOptions(timeout=60_000),
            ),
        )
        # Scale normalised bboxes back to the ORIGINAL frame size
        return _parse_bboxes((resp.text or "").strip(), orig_w, orig_h)
    except Exception as exc:
        logger.warning(f"Character detection failed for one frame: {exc}")
        return []


def detect_batch(
    frames: List[Tuple[float, Image.Image]],
    max_workers: int = 6,
    *,
    ctx: RunContext,
) -> List[List[CharacterBbox]]:
    """Run detect_in_frame over N frames in parallel. Order-preserving output.

    In mock mode returns empty bboxes for every frame without spawning workers."""
    if ctx.is_mock:
        return [[] for _ in frames]

    results: List[List[CharacterBbox]] = [[] for _ in frames]
    with cf.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {
            pool.submit(detect_in_frame, img, ctx=ctx): i
            for i, (_, img) in enumerate(frames)
        }
        for f in cf.as_completed(futs):
            i = futs[f]
            try:
                results[i] = f.result()
            except Exception as exc:
                logger.warning(f"Detection future failed: {exc}")
                results[i] = []
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Helpers for downstream grid composition
# ─────────────────────────────────────────────────────────────────────────────

def consolidate_character_names(raw_names: List[str], *, ctx: RunContext) -> dict:
    """
    Gemini-Flash-driven name consolidation. Across N frames, the detector
    may describe the same character with slight variations ("pink dog",
    "pink aviator pup", "pink puppy"). This collapses those into canonical
    names so downstream per-character grouping is stable.

    Returns a dict mapping each raw name → its canonical form. In mock mode
    or on failure / trivial input, returns an identity mapping.
    """
    unique = sorted(set(n.strip() for n in raw_names if n and n.strip()))
    if len(unique) <= 1 or ctx.is_mock:
        return {n: n for n in unique}

    prompt = (
        "Below is a list of character descriptions detected across different "
        "frames of the same short video. Multiple descriptions may refer to "
        "the SAME character (e.g. 'pink dog', 'pink aviator pup', 'pink puppy' "
        "are all one character). Group them by identity and return a JSON "
        "object mapping each input string to a short canonical name.\n\n"
        "Rules:\n"
        "- Canonical name is a short, distinctive label (e.g. 'pink dog', "
        "  'blue police dog', 'red robot', 'human girl').\n"
        "- If two descriptions refer to clearly different characters, give "
        "  them different canonical names.\n"
        "- Respond with JSON only, no prose. Example:\n"
        '  {"pink dog": "pink dog", "pink aviator pup": "pink dog", '
        '"blue police dog": "blue police dog"}\n\n'
        f"Input descriptions: {json.dumps(unique)}"
    )
    try:
        resp = call_model(
            ctx=ctx,
            role=ModelRole.DETECTION,
            agent_id="detection::name_consolidation",
            contents=[prompt],
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
                http_options=types.HttpOptions(timeout=60_000),
            ),
        )
        text = (resp.text or "").strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            text = m.group(0)
        mapping = json.loads(text)
        if not isinstance(mapping, dict):
            return {n: n for n in unique}
        out = {}
        for raw in unique:
            canonical = mapping.get(raw, raw)
            out[raw] = str(canonical).strip() or raw
        return out
    except Exception as exc:
        logger.warning(f"Character-name consolidation failed: {exc} — using raw names")
        return {n: n for n in unique}


def largest_body_bbox(bboxes: List[CharacterBbox]) -> Optional[Tuple[int, int, int, int]]:
    """Pick the bbox with the largest area — heuristic for 'primary character'."""
    if not bboxes:
        return None
    return max(bboxes, key=lambda b: (b.body[2] - b.body[0]) * (b.body[3] - b.body[1])).body


def largest_face_bbox(bboxes: List[CharacterBbox]) -> Optional[Tuple[int, int, int, int]]:
    """Pick the face of the primary (largest-body) character, if face detected."""
    if not bboxes:
        return None
    primary = max(bboxes, key=lambda b: (b.body[2] - b.body[0]) * (b.body[3] - b.body[1]))
    return primary.face


def eye_region_from_face(
    face_bbox: Tuple[int, int, int, int],
    frame_w: int,
    frame_h: int,
) -> Tuple[int, int, int, int]:
    """
    Given a face bbox, return a bbox cropped to just the EYE region:
    upper 55% of the face, horizontally inset 5%. This gives the facial-
    topology audit 3-5× more pixels on the actual eye structure than the
    whole-face crop, which is what aspect-ratio and curvature analysis needs.
    """
    x1, y1, x2, y2 = face_bbox
    w, h = x2 - x1, y2 - y1
    ex1 = max(0,        int(x1 + 0.05 * w))
    ex2 = min(frame_w,  int(x2 - 0.05 * w))
    ey1 = max(0,        int(y1 + 0.15 * h))  # skip forehead
    ey2 = min(frame_h,  int(y1 + 0.65 * h))  # stop before nose/mouth
    if ex2 - ex1 < 10 or ey2 - ey1 < 10:
        return face_bbox
    return (ex1, ey1, ex2, ey2)


def crop_with_padding(
    frame_pil: Image.Image,
    bbox: Optional[Tuple[int, int, int, int]],
    padding: float = 0.15,
    fallback_region: Tuple[float, float, float, float] = (0.20, 0.25, 0.80, 0.95),
) -> Image.Image:
    """
    Crop frame to bbox with fractional padding. If bbox is None, use the
    fractional fallback_region (x1_frac, y1_frac, x2_frac, y2_frac).
    """
    w, h = frame_pil.size
    if bbox is None:
        x1 = int(w * fallback_region[0]); y1 = int(h * fallback_region[1])
        x2 = int(w * fallback_region[2]); y2 = int(h * fallback_region[3])
    else:
        bx1, by1, bx2, by2 = bbox
        bw, bh = bx2 - bx1, by2 - by1
        px, py = int(bw * padding), int(bh * padding)
        x1 = max(0, bx1 - px); y1 = max(0, by1 - py)
        x2 = min(w, bx2 + px); y2 = min(h, by2 + py)
    if x2 - x1 < 20 or y2 - y1 < 20:
        return frame_pil
    return frame_pil.crop((x1, y1, x2, y2))
