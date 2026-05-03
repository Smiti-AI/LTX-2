#!/usr/bin/env python3
"""
merge_skye_loras.py

Linear-combination merge of two LTX-2 LoRA `.safetensors` checkpoints into a
single LoRA file usable by `run_bm_v1_lora.py` / `inference.py`.

Mathematics
-----------
Each input LoRA contributes a delta `delta_i = B_i @ A_i` (rank r_i).
A linear combination `delta_merged = w_v * delta_v + w_m * delta_m` is
expressible exactly as a single LoRA of rank r_v + r_m via:

    A_merged = cat([w_v * A_v, w_m * A_m], dim=0)   # (r_v + r_m, in_dim)
    B_merged = cat([B_v, B_m], dim=1)               # (out_dim, r_v + r_m)

This is what `peft.add_weighted_adapter(..., combination_type="linear")` does.
We do it directly on the saved state-dicts (the two trained `.safetensors` files
already carry only the LoRA delta tensors) to avoid loading the 22B base model
just to merge adapters.

Usage
-----
    uv run python scripts/merge_skye_loras.py \\
        --lora-a outputs/skye_stills_from_golden_v1/checkpoints/lora_weights_step_03000.safetensors \\
        --lora-b outputs/skye_golden_v2_motion_voice_v1/checkpoints/lora_weights_step_08000.safetensors \\
        --weights "1.0,1.0;0.7,1.0;1.0,0.7;1.2,1.0" \\
        --out-dir outputs/skye_combined_v1/

Produces (one per weight pair):
    outputs/skye_combined_v1/lora_v{wv}_m{wm}.safetensors

Each output is a stand-alone LoRA `.safetensors` with the same key naming
convention as a normally-trained checkpoint (`diffusion_model.<...>.lora_A.weight`,
`diffusion_model.<...>.lora_B.weight`), so existing inference tooling loads it
without modification.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import torch
from safetensors.torch import load_file, save_file

LORA_A_RE = re.compile(r"\.lora_A\.weight$")
LORA_B_RE = re.compile(r"\.lora_B\.weight$")


def _strip_module_prefix(key: str) -> tuple[str, str]:
    """Split a state-dict key into (module_path, suffix) where suffix is
    `.lora_A.weight` or `.lora_B.weight`."""
    if LORA_A_RE.search(key):
        return LORA_A_RE.sub("", key), ".lora_A.weight"
    if LORA_B_RE.search(key):
        return LORA_B_RE.sub("", key), ".lora_B.weight"
    return key, ""


def _group_by_module(state_dict: dict[str, torch.Tensor]) -> dict[str, dict[str, torch.Tensor]]:
    """Return {module_path: {".lora_A.weight": tensor, ".lora_B.weight": tensor, ...}}.

    Non-LoRA tensors (if any leaked into the file) are kept under the empty suffix
    so we don't silently drop them.
    """
    grouped: dict[str, dict[str, torch.Tensor]] = {}
    for k, v in state_dict.items():
        mod, suffix = _strip_module_prefix(k)
        grouped.setdefault(mod, {})[suffix] = v
    return grouped


def merge_two(
    sd_a: dict[str, torch.Tensor],
    sd_b: dict[str, torch.Tensor],
    w_a: float,
    w_b: float,
) -> dict[str, torch.Tensor]:
    """Concat-merge two LoRA state dicts. Both must have matching module-path
    sets and matching `.lora_A.weight` / `.lora_B.weight` suffixes per module."""
    a = _group_by_module(sd_a)
    b = _group_by_module(sd_b)

    only_in_a = set(a) - set(b)
    only_in_b = set(b) - set(a)
    if only_in_a or only_in_b:
        raise ValueError(
            "LoRA files target different modules — cannot merge.\n"
            f"  only in A: {sorted(only_in_a)[:5]}{'…' if len(only_in_a) > 5 else ''}\n"
            f"  only in B: {sorted(only_in_b)[:5]}{'…' if len(only_in_b) > 5 else ''}"
        )

    merged: dict[str, torch.Tensor] = {}
    for mod in sorted(a):
        a_pair, b_pair = a[mod], b[mod]

        # Pass through any non-LoRA tensors that happen to appear in both files
        # identically (defensive — should be empty in practice).
        for suffix, tensor in a_pair.items():
            if suffix in {".lora_A.weight", ".lora_B.weight"}:
                continue
            merged[mod + suffix] = tensor

        if ".lora_A.weight" not in a_pair or ".lora_A.weight" not in b_pair:
            raise ValueError(f"Module {mod} missing .lora_A.weight in one of the files")
        if ".lora_B.weight" not in a_pair or ".lora_B.weight" not in b_pair:
            raise ValueError(f"Module {mod} missing .lora_B.weight in one of the files")

        a_lora_A = a_pair[".lora_A.weight"]
        a_lora_B = a_pair[".lora_B.weight"]
        b_lora_A = b_pair[".lora_A.weight"]
        b_lora_B = b_pair[".lora_B.weight"]

        # Sanity: in_dim must match across A_a / A_b; out_dim must match across B_a / B_b
        if a_lora_A.shape[1] != b_lora_A.shape[1]:
            raise ValueError(
                f"{mod}: lora_A in_dim mismatch ({a_lora_A.shape[1]} vs {b_lora_A.shape[1]})"
            )
        if a_lora_B.shape[0] != b_lora_B.shape[0]:
            raise ValueError(
                f"{mod}: lora_B out_dim mismatch ({a_lora_B.shape[0]} vs {b_lora_B.shape[0]})"
            )

        dtype = a_lora_A.dtype
        # Apply the weight to the A side (peft convention for combination_type="linear").
        # Equivalent under matrix product to applying the weight to the B side.
        merged_A = torch.cat([w_a * a_lora_A.to(torch.float32), w_b * b_lora_A.to(torch.float32)], dim=0).to(dtype)
        merged_B = torch.cat([a_lora_B.to(torch.float32), b_lora_B.to(torch.float32)], dim=1).to(dtype)

        merged[mod + ".lora_A.weight"] = merged_A
        merged[mod + ".lora_B.weight"] = merged_B

    return merged


def parse_weight_pairs(spec: str) -> list[tuple[float, float]]:
    pairs: list[tuple[float, float]] = []
    for chunk in spec.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        wv_str, wm_str = chunk.split(",")
        pairs.append((float(wv_str.strip()), float(wm_str.strip())))
    return pairs


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--lora-a", required=True, type=Path, help="Path to LoRA A (e.g. visual anchor).")
    p.add_argument("--lora-b", required=True, type=Path, help="Path to LoRA B (e.g. motion + voice).")
    p.add_argument(
        "--weights",
        required=True,
        help='Semicolon-separated (w_a, w_b) pairs. Example: "1.0,1.0;0.7,1.0;1.0,0.7;1.2,1.0"',
    )
    p.add_argument("--out-dir", required=True, type=Path, help="Output directory for merged LoRAs.")
    p.add_argument(
        "--prefix",
        default="lora",
        help="Output filename prefix. Each file is named '<prefix>_v{wa}_m{wb}.safetensors'.",
    )
    args = p.parse_args()

    print(f"Loading LoRA A: {args.lora_a}")
    sd_a = load_file(str(args.lora_a))
    print(f"  → {len(sd_a)} tensors")
    print(f"Loading LoRA B: {args.lora_b}")
    sd_b = load_file(str(args.lora_b))
    print(f"  → {len(sd_b)} tensors")

    weight_pairs = parse_weight_pairs(args.weights)
    if not weight_pairs:
        print("No weight pairs provided — nothing to do.", file=sys.stderr)
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)

    metadata = {
        "merge_source_a": str(args.lora_a.name),
        "merge_source_b": str(args.lora_b.name),
        "merge_combination_type": "linear",
    }

    for w_a, w_b in weight_pairs:
        merged = merge_two(sd_a, sd_b, w_a, w_b)
        fname = f"{args.prefix}_v{w_a:g}_m{w_b:g}.safetensors"
        out_path = args.out_dir / fname
        save_file(merged, str(out_path), metadata={**metadata, "merge_w_a": str(w_a), "merge_w_b": str(w_b)})
        n_modules = sum(1 for k in merged if k.endswith(".lora_A.weight"))
        print(f"  wrote {out_path}  ({len(merged)} tensors, {n_modules} merged modules, w_a={w_a}, w_b={w_b})")

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
