"""metadata.json schema. Trainer + manage.py + build_dataset.py all import this."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ClipDeletion:
    reason: str = ""
    deleted_at: str = ""


@dataclass
class Clip:
    index: int
    status: str = "active"  # "active" | "deleted"
    source_url: str = ""
    source_episode: str = ""
    source_season: int = 0
    source_scene_number: float = 0.0
    speaker: str = ""
    video: str = ""           # relative path inside dataset, e.g. "videos/0001.mp4"
    video_md5: str = ""
    prompt: str = ""          # caption text, brand-token-substituted (mirrored from prompts/0NNN.txt)
    prompt_path: str = ""
    latent_path: str = ""     # populated by latent stage; empty in pilot
    duration_s: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    deletion: ClipDeletion | None = None


@dataclass
class Source:
    manifest: str = ""
    manifest_md5: str = ""
    raw_footage_root: str = "gs://video_gen_dataset/raw/paw_patrol/"


@dataclass
class Encoding:
    resolution: tuple[int, int] = (960, 544)
    fps: int = 24
    frames_per_clip: int = 49
    codec: str = "libx264"
    container: str = "mp4"


@dataclass
class Captioning:
    source: str = "labelbox_parquets_consolidated"
    version: str = "v1"
    brand_token_format: str = "CHASE_PP / SKYE_PP"


@dataclass
class Stats:
    active_clips: int = 0
    deleted_clips: int = 0
    total_duration_s: float = 0.0
    missing_captions: int = 0
    download_failures: int = 0


@dataclass
class Gallery:
    path: str = "gallery/gallery.mp4"
    interclip_black_s: float = 0.5
    rendered_at: str = ""
    rendered_index_count: int = 0


@dataclass
class DatasetMetadata:
    dataset_id: str
    character: str
    version: int = 1
    created_at: str = ""
    git_commit: str = ""
    source: Source = field(default_factory=Source)
    encoding: Encoding = field(default_factory=Encoding)
    captioning: Captioning = field(default_factory=Captioning)
    stats: Stats = field(default_factory=Stats)
    clips: list[Clip] = field(default_factory=list)
    gallery: Gallery = field(default_factory=Gallery)

    # ---- I/O ----
    def to_json(self) -> str:
        return json.dumps(_strip_none(asdict(self)), indent=2, ensure_ascii=False)

    def write(self, path: Path) -> None:
        path.write_text(self.to_json())

    @classmethod
    def read(cls, path: Path) -> "DatasetMetadata":
        d = json.loads(path.read_text())
        # Manual rebuild of nested dataclasses (no pydantic dep).
        clips = []
        for c in d.get("clips", []):
            del_block = c.pop("deletion", None)
            clip = Clip(**{k: v for k, v in c.items() if k in Clip.__dataclass_fields__})
            if del_block:
                clip.deletion = ClipDeletion(**del_block)
            clips.append(clip)
        d["clips"] = clips
        d["source"] = Source(**(d.get("source") or {}))
        d["encoding"] = Encoding(**(d.get("encoding") or {}))
        d["captioning"] = Captioning(**(d.get("captioning") or {}))
        d["stats"] = Stats(**(d.get("stats") or {}))
        d["gallery"] = Gallery(**(d.get("gallery") or {}))
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _strip_none(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_none(x) for x in obj]
    if isinstance(obj, tuple):
        return list(obj)
    return obj
