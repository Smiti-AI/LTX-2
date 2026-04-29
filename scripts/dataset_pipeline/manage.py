#!/usr/bin/env python3
"""Mutation tool for a built dataset.

Examples:
  # Mark clip 42 deleted, move files to _trash, re-concat gallery
  python manage.py delete --dataset DATASET_DIR --index 42 --reason "low quality"

  # Multiple at once (commas + ranges)
  python manage.py delete --dataset DATASET_DIR --indices 5,17,42,89-92

  # Restore from _trash if still present
  python manage.py restore --dataset DATASET_DIR --index 42

  # Show summary
  python manage.py list --dataset DATASET_DIR

  # Re-concat gallery (after deletion or manual edits)
  python manage.py rebuild-gallery --dataset DATASET_DIR
"""
from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sys
from pathlib import Path

from gallery import build_concat_list, concat_gallery
from schema import ClipDeletion, DatasetMetadata


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_indices(spec: str) -> list[int]:
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return sorted(out)


def append_audit(dataset: Path, msg: str) -> None:
    log = dataset / "audit" / "deletions.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as f:
        f.write(f"[{utc_now()}] {msg}\n")


def rebuild_gallery(md: DatasetMetadata, dataset: Path) -> int:
    per_clip_dir = dataset / "gallery" / "per_clip"
    concat_list = dataset / "gallery" / "concat.txt"
    out = dataset / "gallery" / "gallery.mp4"
    active = [c.index for c in md.clips if c.status == "active" and c.prompt]
    build_concat_list(active, per_clip_dir, concat_list)
    concat_gallery(concat_list, out)
    md.gallery.rendered_at = utc_now()
    md.gallery.rendered_index_count = sum(1 for i in active if (per_clip_dir / f"{i:04d}.mp4").exists())
    return md.gallery.rendered_index_count


def cmd_delete(args: argparse.Namespace) -> int:
    dataset = Path(args.dataset).resolve()
    md_path = dataset / "metadata.json"
    md = DatasetMetadata.read(md_path)

    indices: list[int] = []
    if args.index is not None:
        indices = [args.index]
    elif args.indices:
        indices = parse_indices(args.indices)
    else:
        print("ERROR: provide --index N or --indices SPEC", file=sys.stderr)
        return 2

    by_idx = {c.index: c for c in md.clips}
    trash = dataset / "_trash" / utc_now().replace(":", "")
    trash.mkdir(parents=True, exist_ok=True)

    deleted_now = 0
    for idx in indices:
        if idx not in by_idx:
            print(f"  skip {idx}: not in metadata", file=sys.stderr)
            continue
        clip = by_idx[idx]
        if clip.status == "deleted":
            print(f"  skip {idx}: already deleted")
            continue
        moved_any = False
        for sub, name in [
            ("raw_videos", f"{idx:04d}.mp4"),
            ("processed_videos", f"{idx:04d}.mp4"),
            ("precomputed/vae_latents_video/processed_videos", f"{idx:04d}.pt"),
            ("precomputed/text_prompt_conditions/processed_videos", f"{idx:04d}.pt"),
            ("_qa_render_cache/source/per_clip", f"{idx:04d}.mp4"),
            ("_qa_render_cache/training/per_clip", f"{idx:04d}.mp4"),
        ]:
            src = dataset / sub / name
            if src.exists():
                dst_dir = trash / sub
                dst_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst_dir / name))
                moved_any = True
        clip.status = "deleted"
        clip.deletion = ClipDeletion(reason=args.reason, deleted_at=utc_now())
        deleted_now += 1
        append_audit(dataset, f"DELETE idx={idx} reason={args.reason!r} moved_files={moved_any}")
        print(f"  deleted {idx}")

    if deleted_now == 0:
        print("Nothing changed.")
        return 0

    # Refresh stats
    md.stats.active_clips = sum(1 for c in md.clips if c.status == "active")
    md.stats.deleted_clips = sum(1 for c in md.clips if c.status == "deleted")
    md.stats.total_duration_s = sum(c.duration_s for c in md.clips if c.status == "active")

    # Rebuild gallery + prompts/all.csv
    rebuild_gallery(md, dataset)
    _rebuild_all_csv(md, dataset)

    md.write(md_path)
    print(f"\nDataset {dataset.name}: active={md.stats.active_clips}, deleted={md.stats.deleted_clips}")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    dataset = Path(args.dataset).resolve()
    md_path = dataset / "metadata.json"
    md = DatasetMetadata.read(md_path)
    by_idx = {c.index: c for c in md.clips}
    if args.index not in by_idx:
        print(f"ERROR: index {args.index} not in metadata", file=sys.stderr)
        return 2
    clip = by_idx[args.index]
    if clip.status == "active":
        print(f"  {args.index} already active")
        return 0
    # Find newest trash dir containing this clip
    trash_root = dataset / "_trash"
    if not trash_root.exists():
        print("ERROR: no _trash/ to restore from", file=sys.stderr)
        return 3
    candidates = sorted(trash_root.iterdir(), reverse=True)
    restored = False
    for cand in candidates:
        v = cand / "raw_videos" / f"{args.index:04d}.mp4"
        if v.exists():
            for sub, name in [("raw_videos", f"{args.index:04d}.mp4"),
                              ("processed_videos", f"{args.index:04d}.mp4"),
                              ("precomputed/vae_latents_video/processed_videos", f"{args.index:04d}.pt"),
                              ("precomputed/text_prompt_conditions/processed_videos", f"{args.index:04d}.pt"),
                              ("_qa_render_cache/source/per_clip", f"{args.index:04d}.mp4"),
                              ("_qa_render_cache/training/per_clip", f"{args.index:04d}.mp4")]:
                src = cand / sub / name
                if src.exists():
                    dst_dir = dataset / sub
                    dst_dir.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(dst_dir / name))
            restored = True
            break
    if not restored:
        print(f"ERROR: clip {args.index} not found in any _trash/* snapshot", file=sys.stderr)
        return 3
    clip.status = "active"
    clip.deletion = None
    md.stats.active_clips = sum(1 for c in md.clips if c.status == "active")
    md.stats.deleted_clips = sum(1 for c in md.clips if c.status == "deleted")
    rebuild_gallery(md, dataset)
    _rebuild_all_csv(md, dataset)
    md.write(md_path)
    append_audit(dataset, f"RESTORE idx={args.index}")
    print(f"  restored {args.index}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    md = DatasetMetadata.read(Path(args.dataset) / "metadata.json")
    print(f"{md.dataset_id}  character={md.character}  v{md.version}")
    print(f"  active: {md.stats.active_clips}   deleted: {md.stats.deleted_clips}")
    print(f"  total active duration: {md.stats.total_duration_s:.1f}s")
    print(f"  gallery: {md.gallery.path}  ({md.gallery.rendered_index_count} indices rendered)")
    if args.show_deleted:
        for c in md.clips:
            if c.status == "deleted" and c.deletion:
                print(f"  [{c.index:04d}] DELETED  {c.deletion.deleted_at}  {c.deletion.reason}")
    return 0


def cmd_rebuild_gallery(args: argparse.Namespace) -> int:
    dataset = Path(args.dataset).resolve()
    md_path = dataset / "metadata.json"
    md = DatasetMetadata.read(md_path)
    n = rebuild_gallery(md, dataset)
    md.write(md_path)
    print(f"rebuilt {dataset / md.gallery.path} with {n} indices")
    return 0


def _rebuild_all_csv(md: DatasetMetadata, dataset: Path) -> None:
    csv = dataset / "prompts" / "all.csv"
    csv.parent.mkdir(parents=True, exist_ok=True)
    with csv.open("w") as f:
        f.write("index,prompt\n")
        for c in md.clips:
            if c.status == "active" and c.prompt:
                p = c.prompt.replace('"', '""').replace("\n", " ").strip()
                f.write(f'{c.index},"{p}"\n')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("delete")
    d.add_argument("--dataset", required=True)
    d.add_argument("--index", type=int)
    d.add_argument("--indices", help="comma list with optional ranges, e.g. 5,17,42,89-92")
    d.add_argument("--reason", default="(no reason given)")
    d.set_defaults(func=cmd_delete)

    r = sub.add_parser("restore")
    r.add_argument("--dataset", required=True)
    r.add_argument("--index", type=int, required=True)
    r.set_defaults(func=cmd_restore)

    ls = sub.add_parser("list")
    ls.add_argument("--dataset", required=True)
    ls.add_argument("--show-deleted", action="store_true")
    ls.set_defaults(func=cmd_list)

    rg = sub.add_parser("rebuild-gallery")
    rg.add_argument("--dataset", required=True)
    rg.set_defaults(func=cmd_rebuild_gallery)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
