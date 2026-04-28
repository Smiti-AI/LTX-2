"""Caption resolver — looks up `scene_caption` for a clip URL across all
known parquet sources for the Golden Set.

Tested empirically (2026-04-28) to provide 100% coverage for both
chase_golden.json (605/605) and skye_golden.json (467/467) when all four
parquets below are downloaded.

Returns a single string (the prose `scene_caption`), brand-token-substituted
if requested. The `transcript_text` (spoken dialogue) is intentionally
ignored — it's a Step 2/3 (audio/dialogue, frozen) concern.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# All parquets that together cover Chase + Skye Golden Sets. Order doesn't
# matter; first-seen URL wins on duplicates.
PARQUET_SOURCES = [
    "gs://video_gen_dataset/dataset/labelbox/Project_Lipsync/Chase/chase_all_seasoned_results_filtered.parquet",
    "gs://video_gen_dataset/dataset/labelbox/Project_Lipsync/Other/Golden_dataset/skye_all_seasons_results_filtered_augmented_no_text_train_20260101_100234.parquet",
    "gs://video_gen_dataset/dataset/labelbox/chase_golden_dataset.parquet",
    "gs://video_gen_dataset/dataset/labelbox/Project_Lipsync/Other/Golden_dataset/skye_golden_dataset.parquet",
]

BRAND_TOKEN_MAP = {
    "chase": "CHASE_PP",
    "skye":  "SKYE_PP",
}


@dataclass
class CaptionEntry:
    url: str
    caption: str
    speaker: str | None
    source_parquet: str


class CaptionIndex:
    """In-memory dict of url → CaptionEntry, built once per build."""

    def __init__(self) -> None:
        self._by_url: dict[str, CaptionEntry] = {}

    def __len__(self) -> int:
        return len(self._by_url)

    def get(self, url: str) -> CaptionEntry | None:
        return self._by_url.get(url)

    def load_parquet(self, local_path: Path, source_uri: str) -> int:
        import pyarrow.parquet as pq
        t = pq.read_table(str(local_path))
        cols = t.column_names
        if "output_video_path" not in cols or "scene_caption" not in cols:
            return 0
        urls = t.column("output_video_path").to_pylist()
        caps = t.column("scene_caption").to_pylist()
        spks = t.column("speaker").to_pylist() if "speaker" in cols else [None] * len(urls)
        added = 0
        for u, c, s in zip(urls, caps, spks):
            if not u or not c:
                continue
            if u in self._by_url:
                continue
            self._by_url[u] = CaptionEntry(url=u, caption=c, speaker=s, source_parquet=source_uri)
            added += 1
        return added


def fetch_parquets(cache_dir: Path) -> list[tuple[Path, str]]:
    """Download every PARQUET_SOURCES entry to cache_dir, idempotent.
    Returns [(local_path, gs_uri), ...]."""
    import subprocess
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for uri in PARQUET_SOURCES:
        name = uri.rsplit("/", 1)[-1]
        local = cache_dir / name
        if not local.exists():
            r = subprocess.run(
                ["gcloud", "storage", "cp", uri, str(local)],
                capture_output=True, text=True, check=False,
            )
            if r.returncode != 0:
                # parquet may be missing in some buckets; skip but flag
                print(f"WARN: failed to fetch {uri}: {r.stderr.strip().splitlines()[-1] if r.stderr else '?'}")
                continue
        out.append((local, uri))
    return out


def build_caption_index(cache_dir: Path) -> CaptionIndex:
    idx = CaptionIndex()
    for local, uri in fetch_parquets(cache_dir):
        idx.load_parquet(local, uri)
    return idx


# --- Brand token substitution -----------------------------------------------

def _word_boundary_replace(text: str, needle: str, repl: str) -> str:
    # Case-insensitive whole-word substitution. Preserves surrounding punctuation.
    pattern = re.compile(rf"\b{re.escape(needle)}\b", flags=re.IGNORECASE)
    return pattern.sub(repl, text)


def apply_brand_tokens(caption: str, characters: list[str] | None = None) -> str:
    """Substitute `Chase`/`Skye` (etc.) → `CHASE_PP`/`SKYE_PP` everywhere
    they appear as whole words. Order matters: longer names first to avoid
    clobbering compound substrings (none currently, but defensive)."""
    chars = characters or list(BRAND_TOKEN_MAP.keys())
    chars = sorted(chars, key=len, reverse=True)
    out = caption
    for c in chars:
        out = _word_boundary_replace(out, c, BRAND_TOKEN_MAP[c])
    return out
