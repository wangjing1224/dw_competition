"""独立资料库及执行进度路径；旧分类读写入口仅供尚待迁移的审核侧使用。"""

from pathlib import Path

from app.core.config import AppSettings, get_settings
from app.models.evidence import EvidenceClassification, EvidenceLibrary
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


def classification_path(
    file_id: str,
    settings: AppSettings | None = None,
) -> Path:
    resolved = settings or get_settings()
    return resolved.storage.data_root / "parsed" / "evidence" / f"{file_id}.json"


def write_evidence_classification(
    file_id: str,
    classification: EvidenceClassification,
    settings: AppSettings | None = None,
) -> Path:
    path = classification_path(file_id, settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        classification.model_dump_json(indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def load_evidence_classification(
    file_id: str,
    settings: AppSettings | None = None,
) -> EvidenceClassification | None:
    path = classification_path(file_id, settings)
    if not path.exists():
        return None
    return EvidenceClassification.model_validate_json(path.read_text("utf-8"))
