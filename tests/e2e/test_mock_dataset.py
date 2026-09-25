"""真实解析/索引/工具/导出，假模型验证新规则与资料库审核全链路。"""

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from langchain_core.messages import AIMessage

from app.evidence.storage import load_library
from app.graph.audit_task_graph import LangGraphAuditExecutor
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


class RouteModel:
    async def ainvoke(self, messages, **kwargs):
        import json

        text = messages[1].content
        status = "not_matched" if "机械加工" in text else "matched"
        return AIMessage(
            content=json.dumps(
                {"match_status": status, "match_reason": "根据采购对象判断"},
                ensure_ascii=False,
            )
        )


class AuditModel:
    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, **kwargs):
        import re

        if any(m.type == "tool" for m in messages):
            return AIMessage(content="处理完毕")
        text = messages[1].content
        report = text[text.index("# 当前投标文件审核报告") :]
        report = report.split("\n## 续读")[0] if "\n## 续读" in report else report
        # 使用实际初始化的骨架，不绕过 report_document 或报告校验。
        headings = re.findall(r"^#{1,3} .+$", report, re.MULTILINE)
        parts = []
        for heading in headings:
            if heading.startswith("### rule-"):
                parts.extend(
                    [heading, "结论：风险\n审核说明：报价待完善，技术响应已有说明。"]
                )
            elif heading == "## 总体总结" or heading == "### 分类总结":
                parts.extend([heading, "存在待完善项，需要复核。"])
            elif heading == "# 当前投标文件审核报告" or re.search(
                r"（[a-z_]+）", heading
            ):
                parts.append(heading)
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "report_document",
                    "id": "save-report",
                    "args": {
                        "action": "write",
                        "content": "\n\n".join(parts) + "\n",
                        "edits": None,
                    },
                    "type": "tool_call",
                }
            ],
        )


@pytest.mark.asyncio
async def test_mock_dataset_new_libraries_through_route_agent_and_reports(
    monkeypatch, temp_settings
):
    monkeypatch.setattr(
        "app.storage.database.database_path",
        lambda: temp_settings.storage.database_path,
    )
    monkeypatch.setattr("app.storage.file_store.get_settings", lambda: temp_settings)
    monkeypatch.setattr("app.parsers.service.get_settings", lambda: temp_settings)
    monkeypatch.setattr("app.retrieval.store.get_settings", lambda: temp_settings)
    initialize_database(temp_settings.storage.database_path)

    class RuleModel:
        submitted = False

        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages, **kwargs):
            assert kwargs["max_tokens"] > 0
            if self.submitted:
                return AIMessage(content="处理完毕")
            self.submitted = True
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "submit_rule_delta",
                        "id": "call-1",
                        "args": {
                            "project_profile": "某省电网有限公司采购红外热成像仪",
                            "return_view": "changed",
                            "operations": [
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
                        },
                        "type": "tool_call",
                    }
                ],
            )

    class MaterialModel:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages, **kwargs):
            if any(message.type == "tool" for message in messages):
                return AIMessage(content="处理完毕")
            prompt = next(
                str(message.content) for message in messages if message.type == "human"
            )
            technical = "技术参数表" in prompt
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "save_evidence_material",
                        "id": "save",
                        "type": "tool_call",
                        "args": {
                            "material": {
                                "bid_summary": "产品技术参数表"
                                if technical
                                else "红外热成像仪供货合同",
                                "evidence_summary": "技术参数表列明探测器分辨率为640x480"
                                if technical
                                else "合同正文包含主体和红外热成像仪标的",
                                "relations": [],
                            },
                            "file_ids": None,
                        },
                    }
                ],
            )

    embedding = MockEmbedding(temp_settings.embedding_model.dimension)
    models = SimpleNamespace(
        rule_extraction=RuleModel(),
        rerank=object(),
        evidence_classification=MaterialModel(),
        embedding=embedding,
    )
    project = await ProjectService(temp_settings, models).create_project(
        folder_uploads(DATA_ROOT / "招标文件")
    )
    bidder = await BidderService(temp_settings, models).create_bidder(
        folder_uploads(DATA_ROOT / "投标方资料"),
    )
    library = load_library(bidder.bidder_id, temp_settings)
    assert len(library.materials) == len(folder_uploads(DATA_ROOT / "投标方资料"))
    assert bidder.status == "evidence_ready"

    runner = BackgroundRunner()
    task_service = AuditTaskService(
        temp_settings,
        embedding,
        LangGraphAuditExecutor(
            temp_settings,
            SimpleNamespace(
                bid_chat=AuditModel(),
                evidence_chat=AuditModel(),
                embedding=embedding,
                rerank=object(),
            ),
        ),
        ReportService(file_store=FileStore()),
        runner,
    )
    task_service.summary_model = RouteModel()
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
