"""把资料细分类聚合成与规则分类一致的主 Agent 精简目录。"""

from app.core.config import AppSettings
from app.models.common import RuleCategory
from app.models.documents import DocumentRef
from app.models.evidence import EvidenceCatalogEntry
from app.evidence.storage import load_evidence_classification


def build_evidence_catalog(
    documents: list[DocumentRef],
    settings: AppSettings,
) -> dict[RuleCategory, EvidenceCatalogEntry]:
    grouped: dict[RuleCategory, list] = {
        category: [] for category in RuleCategory
    }
    for document in documents:
        classification = load_evidence_classification(document.file_id, settings)
        if classification is None:
            continue
        for category in classification.catalog_profile.rule_categories:
            grouped[RuleCategory(str(category))].append(classification)

    catalog: dict[RuleCategory, EvidenceCatalogEntry] = {}
    for category, items in grouped.items():
        summaries = list(
            dict.fromkeys(item.catalog_profile.summary for item in items)
        )
        topics = list(
            dict.fromkeys(
                topic
                for item in items
                for topic in item.catalog_profile.verifiable_topics
            )
        )
        evidence_types = list(
            dict.fromkeys(item.evidence_type for item in items)
        )
        catalog[category] = EvidenceCatalogEntry(
            available=bool(items),
            count=len(items),
            summary="；".join(summaries[:5]) if items else "未提供对应资料",
            evidence_types=evidence_types,
            verifiable_topics=topics[:20],
        )
    return catalog
