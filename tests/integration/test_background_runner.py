"""审核任务从上传到 JSON 输出和 completed 状态的后台生命周期测试。"""

from io import BytesIO

import numpy as np
import pytest

from app.models.results import AuditResult, FileAuditResult, ProjectMatch
from app.models.rules import RuleSet
from app.reports.service import ReportService
from app.services.audit_task_service import AuditTaskService
from app.services.background_runner import BackgroundRunner
from app.storage.database import initialize_database
from app.storage.file_store import FileStore
from app.storage.repositories import (
    AuditTaskRepository,
    BidderRepository,
    ProjectRepository,
)


class FakeEmbedding:
    def __init__(self, dimension):
        self.dimension = dimension

    async def embed(self, texts):
        if not texts:
            return np.empty((0, self.dimension), dtype=np.float32)
        matrix = np.ones((len(texts), self.dimension), dtype=np.float32)
        return matrix / np.linalg.norm(matrix, axis=1, keepdims=True)


class NotApplicableExecutor:
    async def run(self, context):
        file_result = FileAuditResult(
            file=context.bid_files[0],
            is_applicable=False,
            project_match=ProjectMatch(
                status="not_matched",
                reason="测试文件与项目物资类别不匹配",
            ),
            exclusion_reason="物资类别不匹配",
            overall_conclusion="not_applicable",
        )
        return AuditResult(
            task_id=context.task_id,
            project_id=context.project_id,
            bidder_id=context.bidder_id,
            file_results=[file_result],
        )


@pytest.mark.asyncio
async def test_background_task_reaches_completed(monkeypatch, temp_settings):
    monkeypatch.setattr(
        "app.storage.database.database_path",
        lambda: temp_settings.storage.database_path,
    )
    monkeypatch.setattr(
        "app.storage.file_store.get_settings", lambda: temp_settings
    )
    monkeypatch.setattr(
        "app.parsers.service.get_settings", lambda: temp_settings
    )
    monkeypatch.setattr(
        "app.retrieval.store.get_settings", lambda: temp_settings
    )
    initialize_database(temp_settings.storage.database_path)

    projects = ProjectRepository()
    project = projects.create()
    rule_path = temp_settings.storage.data_root / "parsed" / "rules" / "rule.json"
    rule_path.parent.mkdir(parents=True, exist_ok=True)
    rule_path.write_text(
        RuleSet(project_id=project.project_id).model_dump_json(),
        encoding="utf-8",
    )
    project = projects.update_status(
        project.project_id,
        "rule_ready",
        rule_set_path=rule_path.relative_to(
            temp_settings.storage.data_root
        ).as_posix(),
    )
    bidders = BidderRepository()
    bidder = bidders.create(project.project_id)
    bidders.update_status(project.project_id, bidder.bidder_id, "evidence_ready")

    runner = BackgroundRunner()
    service = AuditTaskService(
        temp_settings,
        FakeEmbedding(temp_settings.embedding_model.dimension),
        NotApplicableExecutor(),
        ReportService(file_store=FileStore()),
        runner,
    )
    task = await service.create_task(
        project.project_id,
        bidder.bidder_id,
        [
            (
                "异类文件.txt",
                "投标测试集/异类文件.txt",
                BytesIO("机械加工设备".encode()),
            )
        ],
    )
    await runner.wait()
    completed = AuditTaskRepository().require(task.task_id)

    assert completed.status == "completed"
    assert completed.json_path
    assert FileStore().resolve_relative(completed.json_path).exists()
    assert completed.word_paths == []
