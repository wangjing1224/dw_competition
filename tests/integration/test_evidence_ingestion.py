"""独立资料库 HTTP 上传、真实解析索引与 Agent 保存闭环，不调用外部模型。"""

import sqlite3
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.core.errors import BusinessError
from app.evidence.storage import load_library
from app.main import app
from app.parsers.service import load_parsed_document
from app.services.bidder_service import BidderService
from app.storage.database import initialize_database, open_db
from app.storage.repositories import BidderRepository, FileRepository


@pytest.fixture
def environment(monkeypatch, temp_settings):
    settings = temp_settings.model_copy(
        update={
            "parsing": temp_settings.parsing.model_copy(
                update={"vision_enabled": False}
            )
        }
    )
    for module in (
        "app.main",
        "app.services.evidence_library_service",
        "app.storage.file_store",
        "app.parsers.service",
        "app.parsers.common",
        "app.retrieval.store",
    ):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.storage.database.database_path", lambda: settings.storage.database_path
    )
    initialize_database(settings.storage.database_path)
    return settings


class Embedding:
    async def embed_query(self, query):
        return (await self.embed([query]))[0]

    def __init__(self, dimension):
        self.dimension, self.calls = dimension, []

    async def embed(self, texts):
        self.calls.append(texts)
        return np.ones((len(texts), self.dimension), dtype=np.float32)


def models_for(settings):
    embedding = Embedding(settings.embedding_model.dimension)

    class MaterialModel:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages, **kwargs):
            # 第一轮进入 Agent 时两份文件的解析和统一索引均已完成。
            files = FileRepository().list_by_scope(
                bidder_id="bidder-1", data_role="evidence_material"
            )
            assert len(files) == 2
            assert all(
                load_parsed_document(file.file_id).schema_version == 2 for file in files
            )
            assert len(embedding.calls) == 1 and len(embedding.calls[0]) == 2
            if any(message.type == "tool" for message in messages):
                return AIMessage(content="处理完毕")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "save_evidence_material",
                        "id": "save",
                        "type": "tool_call",
                        "args": {
                            "material": {
                                "bid_summary": "设备合同资料",
                                "evidence_summary": "合同载明设备型号和数量，待后续按要求核验。",
                                "relations": [],
                            },
                            "file_ids": None,
                        },
                    }
                ],
            )

    return SimpleNamespace(
        embedding=embedding, rerank=object(), evidence_classification=MaterialModel()
    )


def test_upload_and_query_without_project(environment, monkeypatch):
    service = BidderService(environment, models_for(environment))
    monkeypatch.setattr("app.api.bidders.build_bidder_service", lambda: service)
    with TestClient(app) as client:
        response = client.post(
            "/api/bidders",
            data={
                "display_name": "测试资料",
                "client_request_id": str(uuid4()),
                "relative_paths": ["合同.md", "说明.md"],
            },
            files=[
                ("files", ("合同.md", "合同：设备20台。".encode(), "text/markdown")),
                (
                    "files",
                    ("说明.md", "对合同的设备型号作说明。".encode(), "text/markdown"),
                ),
            ],
        )
        assert response.status_code == 202, response.text
        from app.services.background_runner import background_runner

        client.portal.call(background_runner.wait)
        assert response.json()["bidder_id"] == "bidder-1"
        result = client.get("/api/bidders/bidder-1").json()
        assert result["bidder"]["status"] == "evidence_ready", result
        assert len(result["library"]["materials"]) == 2
        progress = client.get(
            "/api/bidders/bidder-1/progress?include_trace=true"
        ).json()
        assert progress["completed_file_ids"] == ["file-1", "file-2"]
        assert progress["trace"]["trace_status"] == "completed"
        assert progress["saved_material_count"] == 2
        nodes = {
            change["node"]["span_id"]: change["node"]
            for change in progress["trace"]["changes"]
        }
        saves = [n for n in nodes.values() if n["name"] == "save_evidence_material"]
        assert len(saves) == 2
        trace = client.get(f"/api/bidders/bidder-1/trace/{saves[0]['span_id']}").json()
        assert trace["material_change"]["after"]["provenance"] == "ai_generated"
        assert "provenance" not in trace["input"]["material"]
        assert result["available_for_audit"]
        assert client.post("/api/bidders/bidder-1/files").status_code == 404
        assert client.get("/api/bidders/bidder-404").status_code == 404
    with open_db() as db:
        assert db.execute("SELECT count(*) FROM projects").fetchone()[0] == 0
        rows = db.execute("SELECT * FROM files").fetchall()
        assert all(
            row["project_id"] is None and "bidder-1" in row["storage_path"]
            for row in rows
        )


@pytest.mark.asyncio
async def test_incomplete_upload_cannot_be_resumed_as_smaller_batch(environment):
    service = BidderService(environment, models_for(environment))
    with pytest.raises(BusinessError):
        await service.create_bidder(
            [
                ("ok.md", "ok.md", BytesIO(b"ok")),
                ("bad.exe", "bad.exe", BytesIO(b"bad")),
            ]
        )
    assert BidderRepository().require("bidder-1").error_code == "upload_incomplete"
    with pytest.raises(ValueError, match="上传未完成"):
        await service.resume_bidder("bidder-1")
    assert load_library("bidder-1", environment) is None


def test_old_schema_is_not_modified_or_deleted(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE bidders(project_id TEXT, bidder_id TEXT)")
        db.execute("INSERT INTO bidders VALUES ('old-project', 'old-bidder')")
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="旧版"):
        initialize_database(path)
    assert path.read_bytes() == before


def test_restart_distinguishes_incomplete_upload_and_interrupted_processing(
    environment,
):
    repository = BidderRepository()
    uploading = repository.create(status="uploading_evidence")
    processing = repository.create(status="processing_evidence")
    ready = repository.create(status="evidence_ready")
    assert repository.mark_interrupted_ingestions_failed() == 2
    assert repository.require(uploading.bidder_id).error_code == "upload_incomplete"
    assert repository.require(processing.bidder_id).error_code == "process_interrupted"
    assert repository.require(ready.bidder_id).status == "evidence_ready"
    assert repository.mark_interrupted_ingestions_failed() == 0


def test_independent_ingestion_builds_only_its_own_models(environment, monkeypatch):
    from app.services.container import build_bidder_service

    calls = []

    def chat(settings):
        calls.append(settings)
        assert settings is environment.evidence_classification_model
        return object()

    monkeypatch.setattr("app.services.container.get_settings", lambda: environment)
    monkeypatch.setattr("app.llm.factory.build_chat_model", chat)
    monkeypatch.setattr("app.llm.factory.build_embedding_model", lambda _: object())
    monkeypatch.setattr("app.llm.factory.RerankModel", lambda _: object())
    service = build_bidder_service()
    assert len(calls) == 1 and service.models.evidence_classification is not None
