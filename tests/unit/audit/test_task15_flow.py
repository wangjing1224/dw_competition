"""Task 15 的规则总量、摘要路由和资料目录回归测试。"""

from types import SimpleNamespace

import pytest

from app.audit.bid_summary import summarize_and_route_bid_document
from app.evidence.catalog import build_evidence_catalog
from app.evidence.storage import write_evidence_classification
from app.models.documents import (
    BidDocumentSummary,
    DocumentRef,
    ParsedDocument,
    ParsedSection,
)
from app.models.evidence import EvidenceClassification
from app.models.rules import RuleItem, RuleSet
from app.rules.policies import limit_rules


def _source():
    return {
        "file_id": "rule-file",
        "file_name": "规则.md",
        "source_path": "规则.md",
    }


def test_rule_limit_is_global_not_per_category():
    rules = [
        RuleItem(
            rule_id=f"rule-{index}",
            status="active",
            category="technical",
            priority="important" if index < 4 else "normal",
            verification_level="bid_only",
            name=f"规则{index}",
            requirement=f"要求{index}",
            check_type="semantic",
            severity="major",
            source=_source(),
        )
        for index in range(8)
    ]

    limited = limit_rules(rules, 5)

    assert len(limited) == 5
    assert {str(rule.category) for rule in limited} == {"technical"}


@pytest.mark.asyncio
async def test_low_confidence_route_is_normalized_to_uncertain():
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
        return BidDocumentSummary(
            file_id="file-1",
            summary="可能属于当前项目",
            document_type="bid_document",
            match_status="matched",
            match_reason="只找到物资名称",
            confidence=0.4,
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
            confidence_threshold=0.65,
        )
    finally:
        module.invoke_structured = original

    assert result.match_status == "uncertain"
    assert "置信度不足" in result.match_reason


def test_evidence_catalog_uses_catalog_profile(temp_settings):
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
        classification_confidence=0.9,
    )
    write_evidence_classification(
        document.file_id,
        EvidenceClassification(
            evidence_type="contract",
            confidence=0.9,
            classification_status="classified",
            reason="供货合同",
            catalog_profile={
                "rule_categories": ["commercial", "performance_capacity"],
                "summary": "红外设备供货合同",
                "verifiable_topics": ["合同主体", "产品"],
            },
            retrieval_profile={
                "document_title": "供货合同",
                "entities": ["红外设备"],
                "keywords": ["供货合同"],
                "searchable_facts": ["合同主体"],
            },
        ),
        temp_settings,
    )

    catalog = build_evidence_catalog([document], temp_settings)

    assert catalog["commercial"].available is True
    assert catalog["performance_capacity"].count == 1
    assert catalog["technical"].available is False
