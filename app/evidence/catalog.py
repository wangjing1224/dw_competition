"""按 evidence_type 生成主 Agent 可见的文件级资料目录。"""

from app.core.config import AppSettings
from app.models.common import EvidenceType
from app.models.documents import DocumentRef
from app.models.evidence import EvidenceCatalogItem
from app.evidence.storage import load_evidence_classification


def build_evidence_catalog(
    documents: list[DocumentRef],
    settings: AppSettings,
) -> dict[EvidenceType, list[EvidenceCatalogItem]]:
    catalog: dict[EvidenceType, list[EvidenceCatalogItem]] = {}
    for document in documents:
        classification = load_evidence_classification(document.file_id, settings)
        if classification is None:
            continue
        evidence_type = EvidenceType(str(classification.evidence_type))
        catalog.setdefault(evidence_type, []).append(
            EvidenceCatalogItem(
                file_id=document.file_id,
                status=classification.classification_status,
                summary=classification.bid_summary,
            )
        )
    return catalog
