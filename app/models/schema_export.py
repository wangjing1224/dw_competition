"""将核心 Pydantic 模型导出为稳定 JSON Schema 文件。"""

import json
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from app.core.config import PROJECT_ROOT
from app.models.evidence import EvidenceClassification, EvidenceReviewResult
from app.models.results import AuditResult
from app.models.rules import RuleSet
from app.models.tasks import BidAgentDecision


SCHEMA_TARGETS: tuple[tuple[Any, str], ...] = (
    (RuleSet, "rule_set.schema.json"),
    (EvidenceClassification, "evidence_classification.schema.json"),
    (BidAgentDecision, "bid_agent_decision.schema.json"),
    (EvidenceReviewResult, "evidence_review_result.schema.json"),
    (AuditResult, "audit_result.schema.json"),
)


def export_schemas(output_dir: Path | None = None) -> list[Path]:
    target_dir = output_dir or PROJECT_ROOT / "schemas"
    target_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for schema_type, file_name in SCHEMA_TARGETS:
        path = target_dir / file_name
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                TypeAdapter(schema_type).json_schema(),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(path)
        paths.append(path)
    return paths


if __name__ == "__main__":
    export_schemas()
