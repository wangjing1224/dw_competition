"""独立资料库读写及执行进度路径。"""

from pathlib import Path

from app.core.config import AppSettings
from app.models.evidence import EvidenceLibrary
from app.parsers.assets import atomic_write


def library_path(bidder_id: str, settings: AppSettings) -> Path:
    import re

    if not re.fullmatch(r"bidder-[A-Za-z0-9_-]+", bidder_id):
        raise ValueError("不安全的投标方 ID")
    return settings.storage.data_root / "parsed" / "evidence" / f"{bidder_id}.json"


def write_library(library: EvidenceLibrary, settings: AppSettings) -> None:
    atomic_write(
        library_path(library.bidder_id, settings),
        library.model_dump_json(indent=2).encode(),
    )


def load_library(bidder_id: str, settings: AppSettings) -> EvidenceLibrary | None:
    path = library_path(bidder_id, settings)
    if not path.exists():
        return None
    library = EvidenceLibrary.model_validate_json(path.read_text("utf-8"))
    if library.bidder_id != bidder_id:
        raise ValueError("资料库归属不符")
    return library


def progress_path(bidder_id: str, settings: AppSettings) -> Path:
    return library_path(bidder_id, settings).with_suffix(".progress.json")
