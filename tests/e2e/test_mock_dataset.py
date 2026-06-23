"""使用 test-datas 执行无需外部模型的完整本地审核闭环。"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.evidence.classifier import EvidenceClassification
from app.models.common import generate_id
from app.models.documents import SourceRef
from app.models.evidence import EvidenceReviewResult
from app.models.findings import Finding
from app.models.results import AuditResult, FileAuditResult, ProjectMatch
from app.parsers.service import load_parsed_document
from app.reports.service import ReportService
from app.rules.extractor import ExtractedRule, RuleExtractionBatch
from app.models.rules import ProjectProfile
from app.services.audit_task_service import AuditTaskService, query_audit_task
from app.services.background_runner import BackgroundRunner
from app.services.bidder_service import BidderService
from app.services.project_service import ProjectService
from app.storage.database import initialize_database
from app.storage.file_store import FileStore
from app.storage.repositories import FileRepository


DATA_ROOT = Path(__file__).resolve().parents[2] / "test-datas"


class MockEmbedding:
    """生成稳定归一化向量，只验证索引流程，不模拟语义质量。"""

    def __init__(self, dimension: int) -> None:
        self.dimension = dimension

    async def embed(self, texts):
        if not texts:
            return np.empty((0, self.dimension), dtype=np.float32)
        matrix = np.zeros((len(texts), self.dimension), dtype=np.float32)
        for row, text in enumerate(texts):
            # 使用文本字节和构造可重复的非零向量。
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


def source_from_parsed(file_id: str) -> SourceRef:
    parsed = load_parsed_document(file_id)
    section = parsed.sections[0]
    return SourceRef(
        file_id=file_id,
        file_name=parsed.document.file_name,
        source_path=parsed.document.source_path,
        section=section.title,
        quote=(section.content or "\n".join(section.tables))[:500],
    )


class MockDatasetAuditExecutor:
    """按 Mock 数据中的明确文本形成结果，验证图外全链路和输出契约。"""

    async def run(self, context) -> AuditResult:
        evidence_records = FileRepository().list_by_scope(
            project_id=context.project_id,
            bidder_id=context.bidder_id,
            data_role="evidence_material",
        )
        technical_evidence = next(
            record
            for record in evidence_records
            if record.evidence_type == "technical_parameter"
        )
        evidence_source = source_from_parsed(technical_evidence.file_id)
        file_results: list[FileAuditResult] = []

        for bid_file in context.bid_files:
            parsed = load_parsed_document(bid_file.file_id)
            content = "\n".join(
                part
                for section in parsed.sections
                for part in [section.content, *section.tables]
                if part
            )
            bid_source = source_from_parsed(bid_file.file_id)

            if "机械加工" in content or "数控机床" in content:
                file_results.append(
                    FileAuditResult(
                        file=bid_file,
                        is_applicable=False,
                        project_match=ProjectMatch(
                            status="not_matched",
                            reason="物资类别为机械加工设备，与红外热成像仪项目不匹配",
                            conflicting_fields=["material_category"],
                        ),
                        exclusion_reason="异类投标文件，不进入规则和资料核验",
                        overall_conclusion="not_applicable",
                    )
                )
                continue

            findings: list[Finding] = []
            evidence_results: list[EvidenceReviewResult] = []
            for rule in context.rule_set.rules:
                if rule.target_field == "detector_pixels":
                    review = EvidenceReviewResult(
                        evidence_result_id=generate_id("evidence-result"),
                        claim_id=generate_id("claim"),
                        task_id=context.task_id,
                        project_id=context.project_id,
                        bidder_id=context.bidder_id,
                        bid_file_id=bid_file.file_id,
                        rule_id=rule.rule_id,
                        status="supported",
                        evidence_found=[evidence_source],
                        summary="技术参数表中的探测器像素为640x480，与投标响应一致",
                    )
                    evidence_results.append(review)
                    findings.append(
                        Finding(
                            finding_id=generate_id("finding"),
                            task_id=context.task_id,
                            project_id=context.project_id,
                            bidder_id=context.bidder_id,
                            bid_file_id=bid_file.file_id,
                            rule_id=rule.rule_id,
                            response_status="found",
                            category=rule.category,
                            severity=rule.severity,
                            result="compliant",
                            title="探测器像素满足要求",
                            description="投标响应和技术参数资料均为640x480",
                            rule_source=rule.source,
                            bid_source=bid_source,
                            evidence_result_id=review.evidence_result_id,
                            confidence=1,
                        )
                    )
                elif rule.target_field == "total_price":
                    findings.append(
                        Finding(
                            finding_id=generate_id("finding"),
                            task_id=context.task_id,
                            project_id=context.project_id,
                            bidder_id=context.bidder_id,
                            bid_file_id=bid_file.file_id,
                            rule_id=rule.rule_id,
                            response_status="found",
                            category=rule.category,
                            severity=rule.severity,
                            result="risk",
                            title="投标总价未填写",
                            description="商务响应章节明确显示投标总价未填写",
                            rule_source=rule.source,
                            bid_source=bid_source,
                            suggestion="补充投标总价并核对报价清单一致性",
                            confidence=1,
                        )
                    )

            file_results.append(
                FileAuditResult(
                    file=bid_file,
                    is_applicable=True,
                    project_match=ProjectMatch(
                        status="matched",
                        reason="项目名称、物资类别和包号与招标项目一致",
                        matched_fields=[
                            "project_name",
                            "material_category",
                            "package_no",
                        ],
                    ),
                    overall_conclusion="risk",
                    findings=findings,
                    evidence_results=evidence_results,
                    suggestions=["补充投标总价并复核最终报价文件"],
                )
            )

        return AuditResult(
            task_id=context.task_id,
            project_id=context.project_id,
            bidder_id=context.bidder_id,
            file_results=file_results,
        )


@pytest.mark.asyncio
async def test_mock_dataset_full_pipeline(monkeypatch, temp_settings):
    """真实落盘、解析、索引、后台审核、JSON 和 Word 均执行。"""

    monkeypatch.setattr(
        "app.storage.database.database_path",
        lambda: temp_settings.storage.database_path,
    )
    monkeypatch.setattr("app.storage.file_store.get_settings", lambda: temp_settings)
    monkeypatch.setattr("app.parsers.service.get_settings", lambda: temp_settings)
    monkeypatch.setattr("app.retrieval.store.get_settings", lambda: temp_settings)
    initialize_database(temp_settings.storage.database_path)

    async def mock_rule_extraction(*_args, **_kwargs):
        return RuleExtractionBatch(
            project_profile=ProjectProfile(
                project_name="某省电网2026年红外热成像仪公开招标采购",
                buyer="某省电网有限公司",
                material_category="红外热成像仪",
            ),
            rules=[
                ExtractedRule(
                    category="technical",
                    priority="critical",
                    verification_level="evidence_check",
                    name="探测器像素要求",
                    requirement="探测器像素不得低于640x480",
                    check_type="parameter_compare",
                    target_field="detector_pixels",
                    operator=">=",
                    expected_value="640x480",
                    severity="fatal",
                    evidence_required=True,
                    required_evidence_types=["technical_parameter"],
                ),
                ExtractedRule(
                    category="commercial",
                    priority="important",
                    verification_level="bid_only",
                    name="投标总价完整性",
                    requirement="投标文件必须填写投标总价",
                    check_type="presence",
                    target_field="total_price",
                    severity="major",
                ),
            ],
        )

    async def mock_classification(_model, messages, *_args, **_kwargs):
        prompt = messages[0].content
        if "技术参数表" in prompt:
            return EvidenceClassification(
                evidence_type="technical_parameter",
                confidence=0.99,
                classification_status="classified",
                reason="内容包含产品型号、像素、测温范围和精度",
                catalog_profile={
                    "rule_categories": ["technical"],
                    "summary": "产品技术参数资料",
                    "verifiable_topics": ["像素", "测温范围", "精度"],
                },
                retrieval_profile={
                    "document_title": "技术参数表",
                    "entities": ["红外热成像仪"],
                    "keywords": ["像素", "测温范围", "精度"],
                    "searchable_facts": ["产品型号", "技术参数"],
                },
            )
        return EvidenceClassification(
            evidence_type="contract",
            confidence=0.99,
            classification_status="classified",
            reason="内容包含合同标的和合同主体",
            catalog_profile={
                "rule_categories": ["commercial", "performance_capacity"],
                "summary": "红外热成像仪供货合同",
                "verifiable_topics": ["合同主体", "合同标的"],
            },
            retrieval_profile={
                "document_title": "供货合同",
                "entities": ["红外热成像仪"],
                "keywords": ["供货合同", "红外热成像仪"],
                "searchable_facts": ["合同主体", "合同标的"],
            },
        )

    monkeypatch.setattr(
        "app.rules.extractor.invoke_structured", mock_rule_extraction
    )
    monkeypatch.setattr(
        "app.evidence.classifier.invoke_structured", mock_classification
    )

    embedding = MockEmbedding(temp_settings.embedding_model.dimension)
    preprocessing_models = SimpleNamespace(
        rule_extraction=object(),
        evidence_classification=object(),
        embedding=embedding,
    )
    project = await ProjectService(
        temp_settings, preprocessing_models
    ).create_project(folder_uploads(DATA_ROOT / "招标文件"))
    bidder = await BidderService(
        temp_settings, preprocessing_models
    ).create_bidder(
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

    assert project.status == "rule_ready"
    assert bidder.status == "evidence_ready"
    assert response["status"] == "completed"
    assert len(response["result"]["file_results"]) == 2
    conclusions = {
        item["file"]["file_name"]: item["overall_conclusion"]
        for item in response["result"]["file_results"]
    }
    assert conclusions["01-红外热成像仪投标文件.md"] == "risk"
    assert conclusions["02-机械加工设备投标文件.md"] == "not_applicable"
    assert response["outputs"]["json"]
    assert len(response["outputs"]["word"]) == 2
