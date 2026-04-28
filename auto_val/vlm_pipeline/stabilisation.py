"""
Camera-motion-removal via homography registration.

Problem: a keyframe grid shown to a VLM cannot distinguish
  - "a sign teleported across the scene"   (real defect)
  - "the camera panned and revealed the sign" (normal camera motion)
because both produce the same pixel-level change across panels.

Solution: estimate the frame-to-frame camera motion with ORB + RANSAC
homography, warp every keyframe into the reference frame's coordinate
system, then build the Environment-Stability grid from the STABILISED
frames. In the stabilised view, static background elements occupy the
same pixel positions across all panels; only truly teleporting objects
stand out.

Character bboxes are masked out of feature detection so moving characters
don't corrupt the background-motion estimate.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

import cv2
import numpy as np
from loguru import logger
from PIL import Image


def _pil_to_bgr(img: Image.Image) -> np.ndarray:
    arr = np.array(img)
    if arr.ndim == 2:
        return cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
    if arr.shape[2] == 4:
        arr = arr[:, :, :3]
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


def _bgr_to_pil(arr: np.ndarray) -> Image.Image:
    return Image.fromarray(cv2.cvtColor(arr, cv2.COLOR_BGR2RGB))


def _background_mask(
    shape: Tuple[int, int],
    character_bboxes: List[Tuple[int, int, int, int]],
    padding: float = 0.1,
) -> np.ndarray:
    """
    Build a binary mask that is 1 on the background and 0 on characters.
    ORB features extracted with this mask ignore the character region, so
    character motion doesn't bias the camera-motion estimate.
    """
    h, w = shape
    mask = np.ones((h, w), dtype=np.uint8) * 255
    for bx1, by1, bx2, by2 in character_bboxes:
        bw, bh = bx2 - bx1, by2 - by1
        px, py = int(bw * padding), int(bh * padding)
        x1 = max(0, bx1 - px); y1 = max(0, by1 - py)
        x2 = min(w, bx2 + px); y2 = min(h, by2 + py)
        mask[y1:y2, x1:x2] = 0
    return mask


def _estimate_homography(
    ref_bgr: np.ndarray,
    target_bgr: np.ndarray,
    ref_mask: np.ndarray,
    target_mask: np.ndarray,
    n_features: int = 2000,
) -> Optional[np.ndarray]:
    """
    Estimate the 3×3 homography mapping `target_bgr` → `ref_bgr` coords.
    Returns None if estimation is unstable (too few matches / high residual).
    """
    orb = cv2.ORB_create(nfeatures=n_features, scaleFactor=1.2, nlevels=8)
    ref_gray = cv2.cvtColor(ref_bgr, cv2.COLOR_BGR2GRAY)
    tgt_gray = cv2.cvtColor(target_bgr, cv2.COLOR_BGR2GRAY)

    kp_r, des_r = orb.detectAndCompute(ref_gray, ref_mask)
    kp_t, des_t = orb.detectAndCompute(tgt_gray, target_mask)

    if des_r is None or des_t is None or len(kp_r) < 30 or len(kp_t) < 30:
        return None

    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = bf.match(des_t, des_r)
    if len(matches) < 30:
        return None
    matches = sorted(matches, key=lambda m: m.distance)[:200]

    src_pts = np.float32([kp_t[m.queryIdx].pt for m in matches]).reshape(-1, 1, 2)
    dst_pts = np.float32([kp_r[m.trainIdx].pt for m in matches]).reshape(-1, 1, 2)

    H, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)
    if H is None or mask is None:
        return None
    inliers = int(mask.sum())
    if inliers < 20:
        return None
    # Sanity check: homography should be close to identity-ish; reject
    # pathological warps (huge scaling, perspective collapse).
    if np.any(np.abs(H) > 1e3) or np.abs(np.linalg.det(H[:2, :2])) < 0.05:
        return None
    return H


def stabilise_frames(
    frames: List[Tuple[float, Image.Image]],
    per_frame_bboxes: List[List] = None,
    reference_index: int = 0,
) -> Tuple[List[Tuple[float, Image.Image]], int]:
    """
    Return (stabilised_frames, n_successful_registrations).

    Frame `reference_index` is kept as-is. Every other frame is warped to
    that frame's coordinate system via ORB+RANSAC homography, with character
    bboxes masked out of feature detection.

    If registration fails for a frame, the ORIGINAL frame is used (so we
    degrade gracefully rather than dropping panels).
    """
    if not frames:
        return frames, 0
    if per_frame_bboxes is None:
        per_frame_bboxes = [[] for _ in frames]

    # Convert reference once
    ref_ts, ref_pil = frames[reference_index]
    ref_bgr = _pil_to_bgr(ref_pil)
    ref_h, ref_w = ref_bgr.shape[:2]
    ref_boxes = [b.body for b in per_frame_bboxes[reference_index]] if per_frame_bboxes[reference_index] else []
    ref_mask = _background_mask((ref_h, ref_w), ref_boxes)

    out: List[Tuple[float, Image.Image]] = []
    successes = 0
    for i, (ts, pil_img) in enumerate(frames):
        if i == reference_index:
            out.append((ts, pil_img))
            continue
        tgt_bgr = _pil_to_bgr(pil_img)
        # Resize target to reference dims if different
        if tgt_bgr.shape[:2] != (ref_h, ref_w):
            tgt_bgr = cv2.resize(tgt_bgr, (ref_w, ref_h))
        tgt_boxes = [b.body for b in per_frame_bboxes[i]] if per_frame_bboxes[i] else []
        tgt_mask = _background_mask((ref_h, ref_w), tgt_boxes)

        H = _estimate_homography(ref_bgr, tgt_bgr, ref_mask, tgt_mask)
        if H is None:
            # Fall back to unregistered frame
            out.append((ts, pil_img))
            continue

        warped = cv2.warpPerspective(tgt_bgr, H, (ref_w, ref_h), flags=cv2.INTER_LINEAR)
        out.append((ts, _bgr_to_pil(warped)))
        successes += 1

    return out, successes
