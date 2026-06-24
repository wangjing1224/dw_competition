"""投标摘要路由和文件级资料目录回归测试。"""

from types import SimpleNamespace

import pytest

from app.audit.bid_summary import summarize_and_route_bid_document
from app.evidence.catalog import build_evidence_catalog
from app.evidence.storage import write_evidence_classification
from app.models.documents import BidRouteDecision, DocumentRef, ParsedDocument, ParsedSection
from app.models.evidence import EvidenceClassification
from app.models.rules import RuleSet


@pytest.mark.asyncio
async def test_summary_model_output_is_bound_to_current_file():
    document = DocumentRef(
        file_id="file-1",
        file_name="投标.md",
        source_path="投标.md",
        data_role="bid_document",
        parse_status="parsed",
        project_id="project-1",
        bidder_id="bidder-1",
        task_id="audit-1",
    )
    parsed = ParsedDocument(
        document=document,
        parse_status="parsed",
        sections=[
            ParsedSection(
                section_id="section-1",
                title="投标函",
                content="红外热成像仪投标文件",
            )
        ],
    )

    async def fake_invoke(*_args, **_kwargs):
        return BidRouteDecision(
            summary="## 文件基本信息\n红外热成像仪投标文件",
            match_status="matched",
            match_reason="采购对象一致",
        )

    import app.audit.bid_summary as module

    original = module.invoke_structured
    module.invoke_structured = fake_invoke
    try:
        result = await summarize_and_route_bid_document(
            parsed,
            RuleSet(project_id="project-1"),
            object(),
            SimpleNamespace(structured_output_mode="prompt_only"),
        )
    finally:
        module.invoke_structured = original
    assert result.file_id == "file-1"


def test_evidence_catalog_keeps_file_level_entries(temp_settings):
    document = DocumentRef(
        file_id="file-1",
        file_name="合同.md",
        source_path="合同.md",
        data_role="evidence_material",
        parse_status="parsed",
        project_id="project-1",
        bidder_id="bidder-1",
        evidence_type="contract",
        classification_status="classified",
    )
    write_evidence_classification(
        document.file_id,
        EvidenceClassification(
            evidence_type="contract",
            classification_status="classified",
            reason="正文为供货合同",
            bid_summary="红外设备供货合同，可用于核验业绩",
            evidence_summary="合同主体和标的物清晰可读",
        ),
        temp_settings,
    )
    catalog = build_evidence_catalog([document], temp_settings)
    assert len(catalog["contract"]) == 1
    assert catalog["contract"][0].file_id == "file-1"
