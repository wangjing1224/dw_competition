"""投标方资料分类双视图的轻量 JSON 持久化。"""

from pathlib import Path

from app.core.config import AppSettings, get_settings
from app.models.evidence import EvidenceClassification


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
