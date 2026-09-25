"""路由跳过不会向量化；一个文件失败不抹去其余文件结果。"""

from io import BytesIO
from unittest.mock import AsyncMock

import pytest

from app.models.results import AuditResult
from app.reports.service import ReportService
from app.services.audit_task_service import AuditTaskService, query_audit_task
from app.services.background_runner import BackgroundRunner
from app.storage.database import initialize_database
from app.storage.file_store import FileStore
from app.storage.repositories import BidderRepository, ProjectRepository
from tests.helpers.audit import SequenceModel, audit_context


class RoutedOnlyExecutor:
    async def run(self, context):
        assert not context.bid_files
        return AuditResult(
            task_id=context.task_id,
            project_id=context.project_id,
            bidder_id=context.bidder_id,
            evidence_library=context.evidence_library,
            file_results=context.routed_file_results,
            file_errors=context.file_errors,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_second", [False, True])
async def test_routed_files_get_reports_and_real_errors_are_preserved(
    monkeypatch, temp_settings, fail_second
):
    monkeypatch.setattr(
        "app.storage.database.database_path",
        lambda: temp_settings.storage.database_path,
    )
    monkeypatch.setattr("app.storage.file_store.get_settings", lambda: temp_settings)
    monkeypatch.setattr("app.parsers.service.get_settings", lambda: temp_settings)
    initialize_database(temp_settings.storage.database_path)
    project = ProjectRepository().create()
    ProjectRepository().update_status(project.project_id, "rule_ready")
    bidder = BidderRepository().create()
    BidderRepository().update_status(bidder.bidder_id, "evidence_ready")
    context = audit_context()
    monkeypatch.setattr(
        AuditTaskService,
        "_load_libraries",
        lambda *args: (
            context.rule_set,
            context.evidence_library,
            context.parsed_documents,
        ),
    )
    runner = BackgroundRunner()
    embedding = AsyncMock()
    service = AuditTaskService(
        temp_settings,
        embedding,
        RoutedOnlyExecutor(),
        ReportService(file_store=FileStore()),
        runner,
        summary_model=SequenceModel(
            [
                '{"match_status":"not_matched","match_reason":"采购对象不一致"}',
                RuntimeError("402不足")
                if fail_second
                else '{"match_status":"uncertain","match_reason":"无法确定项目归属"}',
            ]
        ),
    )
    task = await service.create_task(
        project.project_id,
        bidder.bidder_id,
        [
            ("a.txt", "a.txt", BytesIO("机械加工设备".encode())),
            ("b.txt", "b.txt", BytesIO("未知采购项目".encode())),
        ],
    )
    await runner.wait()
    response = query_audit_task(task.task_id)
    assert response["status"] == ("failed" if fail_second else "completed")
    embedding.embed.assert_not_called()
    assert len(response["outputs"]["word"]) == (1 if fail_second else 2)
    assert response["result"]["file_results"][0]["is_applicable"] is False
    if fail_second:
        assert "402不足" in next(iter(response["result"]["file_errors"].values()))
        assert "error" in response
    else:
        assert (
            response["result"]["file_results"][1]["project_match"]["status"]
            == "uncertain"
        )
