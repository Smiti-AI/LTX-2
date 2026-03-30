#!/usr/bin/env python3

"""
Prepare a local folder for Step 3 (process_dataset.py): download videos from GCS and build dataset.json.

Reads a golden Parquet (e.g. skye_golden_dataset_v2.parquet with gs:// output_video_path and scene_caption),
downloads each unique video with gsutil, and writes dataset.json with caption + media_path relative to the
output directory (same layout expected by scripts/process_dataset.py).

Audio (audio-video training)
    Preprocessing uses the **audio track muxed inside each video file** when you pass ``--with-audio`` to
    ``process_dataset.py`` (it loads audio via torchaudio from the same ``media_path`` MP4). Use that for
    joint audio-video LoRA training; your training YAML should set ``training_strategy.with_audio: true``
    and match preprocessed ``audio_latents/``. Optionally use ``--download-separate-audio`` to also fetch
    Parquet ``output_audio_path`` objects (e.g. cut_audio sidecars) under the same folder layout; the stock
    trainer still encodes audio from the **video** files unless you change the pipeline.

Example:

uv run python scripts/prep_data_2_prep_prompts.py \\
    --parquet gs://video_gen_dataset/TinyStories/data_sets/golden_skye/videos/skye_golden_dataset_v2.parquet \\
    --output-dir ~/data/golden_skye_train

Then (audio-video preprocessing):

    uv run python scripts/process_dataset.py ~/data/golden_skye_train/dataset.json \\
      --resolution-buckets "960x544x49" \\
      --model-path /path/to/ltx-2-model.safetensors \\
      --text-encoder-path /path/to/gemma-model \\
      --with-audio

See: https://github.com/Lightricks/LTX-2/blob/main/packages/ltx-trainer/docs/dataset-preparation.md
"""

from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import typer

app = typer.Typer(no_args_is_help=True, help="Download GCS videos + build dataset.json for preprocessing.")


def parse_gs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        msg = f"Expected gs:// URI, got: {uri!r}"
        raise ValueError(msg)
    rest = uri[5:]
    slash = rest.find("/")
    if slash == -1:
        return rest, ""
    return rest[:slash], rest[slash + 1 :]


def gs_to_local_path(gs_uri: str, output_dir: Path, strip_blob_prefix: str) -> Path:
    """Map gs://bucket/blob to output_dir / <relative path after strip>."""
    _, blob = parse_gs_uri(gs_uri)
    if strip_blob_prefix and blob.startswith(strip_blob_prefix):
        rel = blob[len(strip_blob_prefix) :].lstrip("/")
    else:
        rel = Path(blob).name
    if not rel:
        msg = f"Empty relative path for {gs_uri!r}"
        raise ValueError(msg)
    return output_dir / rel


def gsutil_cp(src: str, dst: Path) -> tuple[str, str | None]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["gsutil", "cp", src, str(dst)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        err = e.stderr or e.stdout or str(e)
        return src, err
    return src, None


def load_parquet(path: str) -> pd.DataFrame:
    if path.startswith("gs://"):
        return pd.read_parquet(path)
    p = Path(path)
    if not p.is_file():
        msg = f"File not found: {path}"
        raise FileNotFoundError(msg)
    return pd.read_parquet(p)


@app.command()
def main(
    parquet: str = typer.Option(
        "gs://video_gen_dataset/TinyStories/data_sets/golden_skye/videos/skye_golden_dataset_v2.parquet",
        "--parquet",
        "-p",
        help="Local path or gs:// URI to the golden Parquet manifest",
    ),
    output_dir: Path = typer.Option(
        ...,
        "--output-dir",
        "-o",
        help="Local directory to create; videos and dataset.json are written here",
    ),
    video_column: str = typer.Option(
        "output_video_path",
        "--video-column",
        help="Column with gs:// URIs to MP4 files",
    ),
    caption_column: str = typer.Option(
        "scene_caption",
        "--caption-column",
        help="Column used as training caption (process_dataset caption column)",
    ),
    strip_blob_prefix: str = typer.Option(
        "TinyStories/data_sets/golden_skye/videos/",
        "--strip-blob-prefix",
        help="Strip this object path prefix (after bucket name) to form local paths under --output-dir",
    ),
    dedupe_videos: bool = typer.Option(
        True,
        "--dedupe-videos/--no-dedupe-videos",
        help="Keep one row per unique gs:// video (first caption wins)",
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
        help="Print planned paths only; do not download or write dataset.json",
    ),
    download_separate_audio: bool = typer.Option(
        False,
        "--download-separate-audio",
        help="Also download gs:// URIs from --audio-column (e.g. cut_audio files); same layout as videos",
    ),
    audio_column: str = typer.Option(
        "output_audio_path",
        "--audio-column",
        help="Parquet column for optional separate audio objects (used with --download-separate-audio)",
    ),
) -> None:
    """Create output dir, download videos, write dataset.json for Step 3."""
    df = load_parquet(parquet)
    if video_column not in df.columns:
        typer.echo(f"Missing column {video_column!r}. Columns: {list(df.columns)}", err=True)
        raise typer.Exit(code=1)
    if caption_column not in df.columns:
        typer.echo(f"Missing column {caption_column!r}. Columns: {list(df.columns)}", err=True)
        raise typer.Exit(code=1)

    output_dir = output_dir.expanduser().resolve()
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    # Include audio column when needed so iterrows() can access row[audio_column]
    work_cols = [video_column, caption_column]
    if download_separate_audio and audio_column in df.columns and audio_column not in work_cols:
        work_cols.append(audio_column)

    work_full = df[work_cols].copy()
    work_full = work_full.dropna(subset=[video_column])

    # Rows written to dataset.json
    work_records = (
        work_full.drop_duplicates(subset=[video_column], keep="first")
        if dedupe_videos
        else work_full
    )

    # One download per unique gs:// URI (even if multiple rows share the same clip)
    unique_downloads: dict[str, Path] = {}
    for _, row in work_full.iterrows():
        raw = row[video_column]
        if pd.isna(raw):
            continue
        gs_uri = str(raw).strip()
        if not gs_uri.startswith("gs://"):
            typer.echo(f"Skipping non-gs URI: {gs_uri!r}", err=True)
            continue
        local_path = gs_to_local_path(gs_uri, output_dir, strip_blob_prefix.strip())
        unique_downloads[gs_uri] = local_path

    tasks = list(unique_downloads.items())

    if not tasks:
        typer.echo("No valid gs:// video rows.", err=True)
        raise typer.Exit(code=1)

    audio_list: list[tuple[str, Path]] = []
    if download_separate_audio:
        if audio_column not in df.columns:
            typer.echo(f"Warning: --download-separate-audio but column {audio_column!r} missing.", err=True)
        else:
            audio_unique: dict[str, Path] = {}
            for _, row in work_full.iterrows():
                raw = row[audio_column]
                if pd.isna(raw):
                    continue
                gs_uri_a = str(raw).strip()
                if not gs_uri_a.startswith("gs://"):
                    continue
                audio_unique[gs_uri_a] = gs_to_local_path(gs_uri_a, output_dir, strip_blob_prefix.strip())
            audio_list = list(audio_unique.items())

    typer.echo(f"Parquet: {parquet}")
    typer.echo(f"Output dir: {output_dir}")
    typer.echo(
        f"Manifest rows: {len(work_full)}, dataset.json rows: {len(work_records)}, unique downloads: {len(tasks)}"
    )
    if audio_list:
        typer.echo(f"Separate audio objects to download: {len(audio_list)}")

    if dry_run:
        typer.echo("Videos:")
        for gs_uri, lp in tasks[:15]:
            typer.echo(f"  {gs_uri} -> {lp}")
        if len(tasks) > 15:
            typer.echo(f"  ... and {len(tasks) - 15} more")
        if audio_list:
            typer.echo("Separate audio:")
            for gs_uri, lp in audio_list[:10]:
                typer.echo(f"  {gs_uri} -> {lp}")
            if len(audio_list) > 10:
                typer.echo(f"  ... and {len(audio_list) - 10} more")
        raise typer.Exit(code=0)

    errors: list[tuple[str, str]] = []
    done = 0
    future_map: dict = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for gs_uri, local_path in tasks:
            fut = ex.submit(gsutil_cp, gs_uri, local_path)
            future_map[fut] = (gs_uri, local_path)
        for fut in as_completed(future_map):
            gs_uri, err = fut.result()
            done += 1
            if err:
                errors.append((gs_uri, err))
            if done % 25 == 0 or done == len(tasks):
                typer.echo(f"Download progress: {done}/{len(tasks)}")

    if errors:
        typer.echo(f"Failed downloads: {len(errors)}", err=True)
        for uri, err in errors[:20]:
            typer.echo(f"  {uri}\n    {err[:400]}", err=True)
        raise typer.Exit(code=1)

    if audio_list:
        typer.echo(f"Downloading separate audio files: {len(audio_list)} unique objects")
        err_a: list[tuple[str, str]] = []
        done_a = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            fmap = {ex.submit(gsutil_cp, g, p): (g, p) for g, p in audio_list}
            for fut in as_completed(fmap):
                src_uri, err = fut.result()
                done_a += 1
                if err:
                    err_a.append((src_uri, err))
                if done_a % 25 == 0 or done_a == len(audio_list):
                    typer.echo(f"Audio download progress: {done_a}/{len(audio_list)}")
        if err_a:
            typer.echo(f"Failed audio downloads: {len(err_a)}", err=True)
            for uri, err in err_a[:15]:
                typer.echo(f"  {uri}\n    {err[:400]}", err=True)
            raise typer.Exit(code=1)

    # Build dataset.json: paths relative to output_dir (parent of dataset.json is output_dir)
    records: list[dict[str, str]] = []
    for _, row in work_records.iterrows():
        raw = row[video_column]
        if pd.isna(raw):
            continue
        gs_uri = str(raw).strip()
        if not gs_uri.startswith("gs://"):
            continue
        cap = row[caption_column]
        if pd.isna(cap):
            cap = ""
        local_path = gs_to_local_path(gs_uri, output_dir, strip_blob_prefix.strip())
        if not local_path.is_file():
            typer.echo(f"Missing file after download: {local_path}", err=True)
            raise typer.Exit(code=1)
        rel = local_path.relative_to(output_dir).as_posix()
        records.append({"caption": str(cap), "media_path": rel})

    out_json = output_dir / "dataset.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)

    typer.echo(f"Wrote {out_json} ({len(records)} items).")
    typer.echo(
        "Audio-video Step 3 (encode audio from each video file): "
        f'uv run python scripts/process_dataset.py "{out_json}" '
        '--resolution-buckets "960x544x49" --model-path ... --text-encoder-path ... --with-audio'
    )


if __name__ == "__main__":
    app()
