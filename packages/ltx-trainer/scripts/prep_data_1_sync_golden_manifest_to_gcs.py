#!/usr/bin/env python3

"""
Copy every video referenced in a golden manifest (JSON or Parquet) to a destination GCS prefix.

JSON (Labelbox): array of `{ "data": { "video": "gs://...", "meta": {...} } }`.

Parquet: expects a column with gs:// video URIs (default `output_video_path` for skye_golden_dataset_v2).

Only unique source videos are copied once; duplicate rows still resolve to the same destination URI in the
output manifest.

Example:

    uv run python packages/ltx-trainer/scripts/prep_data_1_sync_golden_manifest_to_gcs.py \\
      --manifest gs://video_gen_dataset/dataset/labelbox/Project_Lipsync/Other/Golden_dataset/skye_golden_dataset_v2.parquet \\
      --dest gs://video_gen_dataset/TinyStories/data_sets/golden_skye/videos \\
      --video-column output_video_path

Requires `gsutil` on PATH with credentials that can read the manifest and source objects
and write to the destination prefix. Parquet requires `pyarrow` (declared in ltx-trainer).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import typer

app = typer.Typer(no_args_is_help=True, help="Sync golden manifest videos to a GCS prefix.")


def parse_gs_uri(uri: str) -> tuple[str, str]:
    """Return (bucket, object_name) for gs://bucket/object."""
    if not uri.startswith("gs://"):
        msg = f"Expected gs:// URI, got: {uri!r}"
        raise ValueError(msg)
    rest = uri[5:]
    slash = rest.find("/")
    if slash == -1:
        return rest, ""
    return rest[:slash], rest[slash + 1 :]


def normalize_dest_prefix(dest: str) -> str:
    """Accept 'bucket/path' or 'gs://bucket/path'; return gs:// URI without trailing slash."""
    d = dest.strip()
    if not d.startswith("gs://"):
        d = f"gs://{d}"
    return d.rstrip("/")


def source_to_dest_object(source_uri: str, dest_prefix: str, strip_prefix: str) -> str:
    """
    Map source object URI to full destination gs:// URI.

    By default strips `dataset/` from the blob path so copies land under
    `.../golden_skye/paw_patrol/...` instead of `.../golden_skye/dataset/paw_patrol/...`.
    """
    _, blob = parse_gs_uri(source_uri)
    if strip_prefix and blob.startswith(strip_prefix):
        rel = blob[len(strip_prefix) :].lstrip("/")
    else:
        rel = blob
    if not rel:
        msg = f"Empty relative path after strip for {source_uri!r}"
        raise ValueError(msg)
    return f"{dest_prefix}/{rel}"


def gsutil_cat(uri: str) -> bytes:
    return subprocess.check_output(["gsutil", "cat", uri], stderr=subprocess.PIPE)


def gsutil_cp(src: str, dst: str) -> tuple[str, str | None]:
    """Copy src to dst. Returns (dst, error_message_or_none)."""
    try:
        subprocess.run(
            ["gsutil", "cp", src, dst],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        err = e.stderr or e.stdout or str(e)
        return dst, err
    return dst, None


def object_exists(uri: str) -> bool:
    try:
        subprocess.run(
            ["gsutil", "ls", uri],
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def ordered_unique_gs_uris(values: list[object]) -> list[str]:
    """First-seen order; skip nulls and non-gs strings."""
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            continue
        u = str(v).strip()
        if not u.startswith("gs://"):
            continue
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def load_json_manifest(manifest_uri: str) -> tuple[list[dict], list[str]]:
    raw = gsutil_cat(manifest_uri)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, list):
        msg = "JSON manifest root must be a JSON array."
        raise ValueError(msg)
    uris: list[str] = []
    for i, row in enumerate(data):
        try:
            src = row["data"]["video"]
        except (KeyError, TypeError) as e:
            msg = f"Record {i}: missing data.video: {e}"
            raise ValueError(msg) from e
        if not isinstance(src, str) or not src.startswith("gs://"):
            msg = f"Record {i}: invalid data.video (expected gs:// string): {src!r}"
            raise ValueError(msg)
        uris.append(src)
    unique = ordered_unique_gs_uris(uris)
    return data, unique


def load_parquet_manifest(manifest_uri: str, video_column: str) -> tuple[pd.DataFrame, list[str]]:
    df = pd.read_parquet(manifest_uri)
    if video_column not in df.columns:
        msg = f"Column {video_column!r} not in parquet columns: {list(df.columns)}"
        raise ValueError(msg)
    col = df[video_column].tolist()
    unique = ordered_unique_gs_uris(col)
    return df, unique


@app.command()
def main(
    manifest: str = typer.Option(
        "gs://video_gen_dataset/dataset/labelbox/Project_Lipsync/Other/Golden_dataset/skye_golden_dataset_v2.parquet",
        "--manifest",
        "-m",
        help="gs:// URI of JSON or Parquet manifest",
    ),
    dest: str = typer.Option(
        "gs://video_gen_dataset/TinyStories/data_sets/golden_skye",
        "--dest",
        "-d",
        help="Destination GCS prefix (bucket/path, with or without gs://)",
    ),
    video_column: str = typer.Option(
        "output_video_path",
        "--video-column",
        help="Parquet column with gs:// video URIs (ignored for Labelbox JSON)",
    ),
    strip_prefix: str = typer.Option(
        "dataset/",
        "--strip-prefix",
        help="Strip this blob prefix when building destination object paths (after bucket)",
    ),
    workers: int = typer.Option(
        16,
        "--workers",
        "-j",
        help="Parallel gsutil cp jobs",
        min=1,
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print planned copies only; do not run gsutil cp",
    ),
    skip_existing: bool = typer.Option(
        False,
        "--skip-existing",
        help="Skip copy if destination object already exists",
    ),
    write_manifest: bool = typer.Option(
        True,
        "--write-manifest/--no-write-manifest",
        help="Upload updated manifest under --dest (JSON or Parquet matching input)",
    ),
    manifest_name: str | None = typer.Option(
        None,
        "--manifest-name",
        help="Output object name under --dest (default: same basename as --manifest)",
    ),
) -> None:
    """Copy unique manifest videos to dest prefix and optionally upload an updated manifest."""
    dest_prefix = normalize_dest_prefix(dest)

    suffix = Path(manifest.split("?", 1)[0]).suffix.lower()
    json_data: list[dict] | None = None
    parquet_df: pd.DataFrame | None = None
    unique_sources: list[str]

    if suffix == ".parquet":
        parquet_df, unique_sources = load_parquet_manifest(manifest, video_column)
        typer.echo(f"Loaded parquet rows: {len(parquet_df)}, unique gs:// videos: {len(unique_sources)}")
    elif suffix in (".json", ".jsonl"):
        if suffix == ".jsonl":
            typer.echo("JSONL: reading line-by-line not implemented; use .json array.", err=True)
            raise typer.Exit(code=1)
        json_data, unique_sources = load_json_manifest(manifest)
        typer.echo(f"Loaded JSON rows: {len(json_data)}, unique gs:// videos: {len(unique_sources)}")
    else:
        typer.echo(
            f"Unsupported manifest extension {suffix!r}; use .parquet or .json",
            err=True,
        )
        raise typer.Exit(code=1)

    if not unique_sources:
        typer.echo("No gs:// video URIs found.", err=True)
        raise typer.Exit(code=1)

    tasks: list[tuple[str, str]] = []
    for src in unique_sources:
        dst_uri = source_to_dest_object(src, dest_prefix, strip_prefix)
        tasks.append((src, dst_uri))

    src_to_dst: dict[str, str] = {s: d for s, d in tasks}

    out_name = manifest_name or Path(manifest.split("?", 1)[0]).name

    typer.echo(f"Manifest: {manifest}")
    typer.echo(f"Destination prefix: {dest_prefix}")
    typer.echo(f"Unique videos to copy: {len(tasks)}")

    if dry_run:
        for src, dst_uri in tasks[:20]:
            typer.echo(f"  {src} -> {dst_uri}")
        if len(tasks) > 20:
            typer.echo(f"  ... and {len(tasks) - 20} more")
        raise typer.Exit(code=0)

    errors: list[tuple[str, str]] = []
    done = 0

    def run_one(pair: tuple[str, str]) -> tuple[str, str | None]:
        src, dst_uri = pair
        if skip_existing and object_exists(dst_uri):
            return dst_uri, None
        return gsutil_cp(src, dst_uri)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        future_map = {ex.submit(run_one, t): t for t in tasks}
        for fut in as_completed(future_map):
            dst_uri, err = fut.result()
            done += 1
            if err:
                src = future_map[fut][0]
                errors.append((src, err))
            if done % 50 == 0 or done == len(tasks):
                typer.echo(f"Progress: {done}/{len(tasks)}")

    if errors:
        typer.echo(f"Failed copies: {len(errors)}", err=True)
        for src, err in errors[:30]:
            typer.echo(f"  {src}\n    {err[:500]}", err=True)
        if len(errors) > 30:
            typer.echo(f"  ... and {len(errors) - 30} more errors", err=True)
        raise typer.Exit(code=1)

    typer.echo("All video copies finished.")

    if write_manifest and json_data is not None:
        for row in json_data:
            src = row["data"]["video"]
            if isinstance(src, str) and src in src_to_dst:
                row["data"]["video"] = src_to_dst[src]

        out_json = json.dumps(json_data, indent=2)
        out_uri = f"{dest_prefix}/{out_name}"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            tmp.write(out_json)
            tmp_path = tmp.name
        try:
            subprocess.run(
                ["gsutil", "cp", tmp_path, out_uri],
                check=True,
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        typer.echo(f"Uploaded updated manifest: {out_uri}")

    if write_manifest and parquet_df is not None:
        col = video_column
        updated = parquet_df.copy()
        def map_video_uri(u: object) -> object:
            if pd.isna(u):
                return u
            s = str(u).strip()
            return src_to_dst.get(s, u)

        updated[col] = updated[col].map(map_video_uri)

        suffix_out = Path(out_name).suffix.lower() or ".parquet"
        with tempfile.NamedTemporaryFile(suffix=suffix_out, delete=False) as tmp:
            tmp_path = tmp.name
        try:
            updated.to_parquet(tmp_path, index=False)
            out_uri = f"{dest_prefix}/{out_name}"
            subprocess.run(
                ["gsutil", "cp", tmp_path, out_uri],
                check=True,
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        typer.echo(f"Uploaded updated manifest: {out_uri}")


if __name__ == "__main__":
    app()
