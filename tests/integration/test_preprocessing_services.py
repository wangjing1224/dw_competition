"""使用真实 SQLite/文件/解析/索引和 Mock 模型验证预处理闭环。"""

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest
from langchain_core.messages import AIMessage
from PIL import Image

from app.models.documents import DocumentRef
from app.models.evidence import EvidenceClassification
from app.parsers.service import prepare_document
from app.retrieval.index import build_document_index
from app.retrieval.models import IndexScope
from app.services.bidder_service import BidderService
from app.services.project_service import ProjectService
from app.storage.database import initialize_database
from app.storage.file_store import FileOwner, FileStore


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

    class RuleModel:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages, **kwargs):
            assert kwargs["extra_body"]["max_tokens"] > 0
            if any(getattr(message, "type", "") == "tool" for message in messages):
                return AIMessage(content="处理完毕")
            return AIMessage(
                content="",
                tool_calls=[
                    dict(
                        name="submit_rule_delta",
                        id="call-1",
                        args=dict(
                            operations=[
                                {
                                    "action": "add",
                                    "category": "technical",
                                    "priority": "important",
                                    "verification": "bid_only",
                                    "requirement": "探测器分辨率不低于640x480",
                                    "reason": "技术规范明确要求",
                                }
                            ],
                            project_profile="本项目采购红外设备",
                            return_view="changed",
                        ),
                        type="tool_call",
                    )
                ],
            )

    async def fake_classification(*_args, **_kwargs):
        return EvidenceClassification(
            evidence_type="contract",
            classification_status="classified",
            reason="包含合同标的和双方主体",
            bid_summary="红外热成像仪供货合同",
            evidence_summary="合同标的为红外热成像仪20台，正文可读",
        )

    monkeypatch.setattr(
        "app.evidence.classifier.invoke_structured",
        fake_classification,
    )
    models = SimpleNamespace(
        rule_extraction=RuleModel(),
        rerank=object(),
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


@pytest.mark.asyncio
async def test_image_only_tender_index_does_not_call_embedding(
    preprocessing_environment,
):
    settings = preprocessing_environment
    from app.storage.repositories import ProjectRepository

    project = ProjectRepository().create()
    buffer = BytesIO()
    Image.new("RGB", (40, 40), "white").save(buffer, format="PNG")
    buffer.seek(0)
    record = FileStore().save(
        owner=FileOwner(project_id=project.project_id),
        data_role="rule_source",
        stream=buffer,
        file_name="扫描.png",
        relative_path="扫描.png",
    )
    document = DocumentRef(
        file_id=record.file_id,
        project_id=project.project_id,
        data_role="rule_source",
        file_name=record.file_name,
        source_path=record.source_path,
    )
    await prepare_document(document, vision_enabled=False)
    embedding = AsyncMock()
    result = await build_document_index(
        IndexScope(
            scope_id=f"rules-{project.project_id}",
            data_role="rule_source",
            project_id=project.project_id,
        ),
        [document],
        embedding,
        settings,
    )
    assert result.chunk_count == 0
    embedding.embed.assert_not_awaited()
