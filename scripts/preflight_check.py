#!/usr/bin/env python3
"""Validate docs/pre_flight.yaml. Run at session start.

Exit 0 if all blocking fields are set. Exit 1 if any blocking field is unset
or the schema is malformed. Outstanding TBDs in non-blocking fields are
reported as warnings.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
PRE_FLIGHT = REPO / "docs" / "pre_flight.yaml"

BLOCKING = [
    "gcp.project",
    "gcp.buckets_to_audit",
    "gcp.auth_method",
    "github.repo",
    "github.branch_strategy",
    "env.training_target",
]

NON_BLOCKING_TBDS_TO_REPORT = [
    "data_paths.golden_set_150",
    "data_paths.high_res_stills_partial",
    "phase0.pp_watermark_templates",
]


def get(d: dict, dotted: str):
    cur = d
    for k in dotted.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def is_unset(v) -> bool:
    if v is None:
        return True
    if isinstance(v, str) and v.strip().upper() == "TBD":
        return True
    if isinstance(v, list) and len(v) == 0:
        return True
    return False


def main() -> int:
    if not PRE_FLIGHT.exists():
        print(f"ERROR: {PRE_FLIGHT} not found. Copy docs/pre_flight.example.yaml to start.")
        return 1
    cfg = yaml.safe_load(PRE_FLIGHT.read_text())
    if not isinstance(cfg, dict):
        print(f"ERROR: {PRE_FLIGHT} did not parse to a mapping.")
        return 1

    blocking_missing = [k for k in BLOCKING if is_unset(get(cfg, k))]
    warnings = [k for k in NON_BLOCKING_TBDS_TO_REPORT if is_unset(get(cfg, k))]

    print(f"pre_flight: {PRE_FLIGHT}")
    print(f"  gcp.project           = {get(cfg, 'gcp.project')}")
    print(f"  gcp.auth_method       = {get(cfg, 'gcp.auth_method')}")
    print(f"  buckets               = {len(get(cfg, 'gcp.buckets_to_audit') or [])}")
    print(f"  branch_strategy       = {get(cfg, 'github.branch_strategy')}")
    print(f"  training_target       = {get(cfg, 'env.training_target')}")
    print()

    if warnings:
        print("WARN: outstanding TBDs (non-blocking, downstream tasks may be limited):")
        for k in warnings:
            print(f"  - {k}")
        print()

    if blocking_missing:
        print("ERROR: blocking fields unset:")
        for k in blocking_missing:
            print(f"  - {k}")
        return 1

    print("OK: all blocking fields set.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
