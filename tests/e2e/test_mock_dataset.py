"""使用 test-datas 执行无需外部模型的完整本地审核闭环。"""

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from langchain_core.messages import AIMessage

from app.models.evidence import EvidenceClassification
from app.models.findings import Finding
from app.models.results import AuditResult, FileAuditResult, ProjectMatch
from app.models.rules import RuleReportItem, present_rule_for_model
from app.parsers.service import load_parsed_document
from app.reports.service import ReportService
from app.services.audit_task_service import AuditTaskService, query_audit_task
from app.services.background_runner import BackgroundRunner
from app.services.bidder_service import BidderService
from app.services.project_service import ProjectService
from app.storage.database import initialize_database
from app.storage.file_store import FileStore

DATA_ROOT = Path(__file__).resolve().parents[2] / "test-datas"


class MockEmbedding:
    def __init__(self, dimension: int) -> None:
        self.dimension = dimension

    async def embed(self, texts):
        if not texts:
            return np.empty((0, self.dimension), dtype=np.float32)
        matrix = np.zeros((len(texts), self.dimension), dtype=np.float32)
        for row, text in enumerate(texts):
            for index, value in enumerate(text.encode("utf-8")):
                matrix[row, index % self.dimension] += value + 1
        return matrix / np.linalg.norm(matrix, axis=1, keepdims=True)


def folder_uploads(path: Path) -> list[tuple[str, str, BytesIO]]:
    return [
        (
            file_path.name,
            file_path.relative_to(path).as_posix(),
            BytesIO(file_path.read_bytes()),
        )
        for file_path in sorted(item for item in path.rglob("*") if item.is_file())
    ]


class MockDatasetAuditExecutor:
    async def run(self, context) -> AuditResult:
        file_results: list[FileAuditResult] = []
        for bid_file in context.bid_files:
            parsed = load_parsed_document(bid_file.file_id)
            content = "\n".join(
                part
                for section in parsed.sections
                for part in [section.content, *section.tables]
                if part
            )
            if "机械加工" in content or "数控机床" in content:
                file_results.append(
                    FileAuditResult(
                        file=bid_file,
                        is_applicable=False,
                        project_match=ProjectMatch(
                            status="not_matched",
                            reason="采购对象为机械加工设备，与当前项目不匹配",
                        ),
                        document_summary=context.bid_summaries[bid_file.file_id],
                        exclusion_reason="采购对象不匹配",
                        overall_conclusion="not_applicable",
                    )
                )
                continue
            findings = [
                Finding(
                    rule_id=rule.rule_id,
                    result=(
                        "risk" if str(rule.category) == "commercial" else "compliant"
                    ),
                    summary=(
                        "投标总价未填写，需要补充"
                        if str(rule.category) == "commercial"
                        else "投标响应明确给出640x480，满足技术要求"
                    ),
                )
                for rule in context.rule_set.rules
            ]
            file_results.append(
                FileAuditResult(
                    file=bid_file,
                    is_applicable=True,
                    project_match=ProjectMatch(
                        status="matched",
                        reason="项目名称和采购对象一致",
                    ),
                    document_summary=context.bid_summaries[bid_file.file_id],
                    overall_conclusion="risk",
                    audit_summary="技术响应满足要求，但报价信息存在缺口",
                    findings=findings,
                    suggestions=["补充投标总价并复核报价文件"],
                )
            )
        return AuditResult(
            task_id=context.task_id,
            project_id=context.project_id,
            bidder_id=context.bidder_id,
            rules=[
                RuleReportItem.model_validate(
                    present_rule_for_model(rule).model_dump(mode="json")
                )
                for rule in context.rule_set.rules
            ],
            file_results=file_results,
        )


@pytest.mark.asyncio
async def test_mock_dataset_full_pipeline(monkeypatch, temp_settings):
    monkeypatch.setattr(
        "app.storage.database.database_path",
        lambda: temp_settings.storage.database_path,
    )
    monkeypatch.setattr("app.storage.file_store.get_settings", lambda: temp_settings)
    monkeypatch.setattr("app.parsers.service.get_settings", lambda: temp_settings)
    monkeypatch.setattr("app.retrieval.store.get_settings", lambda: temp_settings)
    monkeypatch.setattr("app.reports.word_report.get_settings", lambda: temp_settings)
    initialize_database(temp_settings.storage.database_path)

    class RuleModel:
        submitted = False

        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            if self.submitted:
                return AIMessage(content="处理完毕")
            self.submitted = True
            return AIMessage(
                content="",
                tool_calls=[
                    dict(
                        name="submit_rule_delta",
                        id="call-1",
                        args=dict(
                            project_profile="某省电网有限公司采购红外热成像仪",
                            return_view="changed",
                            operations=[
                                {
                                    "action": "add",
                                    "category": "technical",
                                    "priority": "critical",
                                    "verification": "evidence_check",
                                    "requirement": "探测器分辨率不得低于640x480",
                                    "reason": "技术规范明确要求",
                                },
                                {
                                    "action": "add",
                                    "category": "commercial",
                                    "priority": "important",
                                    "verification": "bid_only",
                                    "requirement": "投标文件必须填写投标总价",
                                    "reason": "报价要求明确",
                                },
                            ],
                        ),
                        type="tool_call",
                    )
                ],
            )

    async def mock_classification(_model, messages, *_args, **_kwargs):
        prompt = "\n".join(str(message.content) for message in messages)
        technical = "技术参数表" in prompt
        return EvidenceClassification(
            evidence_type="technical_parameter" if technical else "contract",
            classification_status="classified",
            reason="正文类型明确",
            bid_summary=(
                "产品技术参数表，可用于核验产品参数"
                if technical
                else "红外热成像仪供货合同，可用于核验业绩"
            ),
            evidence_summary=(
                "技术参数表列明探测器分辨率为640x480"
                if technical
                else "合同正文包含主体和红外热成像仪标的"
            ),
        )

    monkeypatch.setattr(
        "app.evidence.classifier.invoke_structured",
        mock_classification,
    )

    embedding = MockEmbedding(temp_settings.embedding_model.dimension)
    models = SimpleNamespace(
        rule_extraction=RuleModel(),
        rerank=object(),
        evidence_classification=object(),
        embedding=embedding,
    )
    project = await ProjectService(temp_settings, models).create_project(
        folder_uploads(DATA_ROOT / "招标文件")
    )
    bidder = await BidderService(temp_settings, models).create_bidder(
        project.project_id,
        folder_uploads(DATA_ROOT / "投标方资料"),
    )

    runner = BackgroundRunner()
    task_service = AuditTaskService(
        temp_settings,
        embedding,
        MockDatasetAuditExecutor(),
        ReportService(file_store=FileStore()),
        runner,
    )
    task = await task_service.create_task(
        project.project_id,
        bidder.bidder_id,
        folder_uploads(DATA_ROOT / "投标文件测试集"),
    )
    await runner.wait()
    response = query_audit_task(task.task_id)

    assert response["status"] == "completed"
    conclusions = {
        item["file"]["file_name"]: item["overall_conclusion"]
        for item in response["result"]["file_results"]
    }
    assert conclusions["01-红外热成像仪投标文件.md"] == "risk"
    assert conclusions["02-机械加工设备投标文件.md"] == "not_applicable"
    assert len(response["outputs"]["word"]) == 2
