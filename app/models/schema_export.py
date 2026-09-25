"""把当前领域模型的 JSON Schema 导出到根目录 schemas/。"""

import json
from pathlib import Path

from app.core.config import PROJECT_ROOT
from app.models.documents import BidRouteDecision
from app.models.evidence import EvidenceClassification, EvidenceLibrary
from app.models.results import AuditResult
from app.models.rules import RuleSet
from app.models.tasks import AgentAuditDecision

SCHEMA_MODELS = {
    "rule_set.schema.json": RuleSet,
    "evidence_classification.schema.json": EvidenceClassification,
    # MD 解析后的内部对象，不绑定给模型。
    "report_decision.schema.json": AgentAuditDecision,
    "evidence_library.schema.json": EvidenceLibrary,
    "bid_route.schema.json": BidRouteDecision,
    "audit_result.schema.json": AuditResult,
}


def export_schemas(output_dir: Path | None = None) -> list[Path]:
    directory = output_dir or PROJECT_ROOT / "schemas"
    directory.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for file_name, model in SCHEMA_MODELS.items():
        path = directory / file_name
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                model.model_json_schema(),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
        paths.append(path)
    return paths


if __name__ == "__main__":
    export_schemas()
