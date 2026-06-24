"""使用真实 SQLite/文件/解析/索引和 Mock 模型验证预处理闭环。"""

from io import BytesIO
from types import SimpleNamespace

import numpy as np
import pytest

from app.models.evidence import EvidenceClassification
from app.models.rules import RuleDeltaResult
from app.services.bidder_service import BidderService
from app.services.project_service import ProjectService
from app.storage.database import initialize_database


class FakeEmbedding:
    def __init__(self, dimension):
        self.dimension = dimension

    async def embed(self, texts):
        if not texts:
            return np.empty((0, self.dimension), dtype=np.float32)
        matrix = np.ones((len(texts), self.dimension), dtype=np.float32)
        return matrix / np.linalg.norm(matrix, axis=1, keepdims=True)


@pytest.fixture
def preprocessing_environment(monkeypatch, temp_settings):
    monkeypatch.setattr(
        "app.storage.database.database_path",
        lambda: temp_settings.storage.database_path,
    )
    monkeypatch.setattr("app.storage.file_store.get_settings", lambda: temp_settings)
    monkeypatch.setattr("app.parsers.service.get_settings", lambda: temp_settings)
    monkeypatch.setattr("app.retrieval.store.get_settings", lambda: temp_settings)
    initialize_database(temp_settings.storage.database_path)
    return temp_settings


@pytest.mark.asyncio
async def test_project_and_bidder_preprocessing_reach_ready(
    monkeypatch, preprocessing_environment
):
    settings = preprocessing_environment

    async def fake_rule_output(*_args, **_kwargs):
        return RuleDeltaResult(
            operations=[
                {
                    "action": "add",
                    "category": "technical",
                    "priority": "important",
                    "verification": "bid_only",
                    "requirement": "探测器分辨率不低于640x480",
                    "reason": "技术规范明确要求",
                }
            ]
        )

    async def fake_classification(*_args, **_kwargs):
        return EvidenceClassification(
            evidence_type="contract",
            classification_status="classified",
            reason="包含合同标的和双方主体",
            bid_summary="红外热成像仪供货合同",
            evidence_summary="合同标的为红外热成像仪20台，正文可读",
        )

    monkeypatch.setattr("app.rules.extractor.invoke_structured", fake_rule_output)
    monkeypatch.setattr(
        "app.evidence.classifier.invoke_structured",
        fake_classification,
    )
    models = SimpleNamespace(
        rule_extraction=object(),
        evidence_classification=object(),
        embedding=FakeEmbedding(settings.embedding_model.dimension),
    )
    project = await ProjectService(settings, models).create_project(
        [
            (
                "规则.txt",
                "示例招标文件/规则.txt",
                BytesIO("技术要求：像素不低于640x480".encode()),
            )
        ]
    )
    bidder = await BidderService(settings, models).create_bidder(
        project.project_id,
        [
            (
                "合同.txt",
                "投标方资料/合同.txt",
                BytesIO("合同标的为红外热成像仪20台".encode()),
            )
        ],
    )
    assert project.status == "rule_ready"
    assert bidder.status == "evidence_ready"
