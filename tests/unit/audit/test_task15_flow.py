"""投标摘要路由和文件级资料目录回归测试。"""

import pytest

from app.audit.bid_summary import summarize_and_route_bid_document
from app.evidence.catalog import build_evidence_catalog
from app.evidence.storage import write_evidence_classification
from app.models.documents import (
    DocumentRef,
)
from app.models.evidence import EvidenceClassification


@pytest.mark.asyncio
async def test_route_output_has_only_two_model_fields(temp_settings):
    from tests.helpers.audit import SequenceModel, audit_context, parsed_file

    model = SequenceModel(['{"match_status":"matched","match_reason":"采购对象一致"}'])
    result = await summarize_and_route_bid_document(
        parsed_file("bid-1"),
        audit_context().rule_set,
        model,
        temp_settings.bid_summary_model,
    )
    assert result.file_id == "bid-1"
    assert "summary" not in result.model_dump()
    assert result.reading_end == result.total_units == 1
    assert "rule-1" not in model.calls[0][0][1].content
    assert model.calls[0][1]["max_tokens"] > 0


@pytest.mark.asyncio
async def test_route_has_one_bounded_repair_and_never_defaults_to_matched(
    temp_settings,
):
    from tests.helpers.audit import SequenceModel, audit_context, parsed_file

    model = SequenceModel(
        ["坏JSON", '{"match_status":"uncertain","match_reason":"项目主体缺失"}']
    )
    result = await summarize_and_route_bid_document(
        parsed_file("bid-1"),
        audit_context().rule_set,
        model,
        temp_settings.bid_summary_model,
    )
    assert result.match_status == "uncertain"
    assert len(model.calls) == 2
    assert "bid-1独立正文" in model.calls[1][0][1].content
    with pytest.raises(RuntimeError, match="402"):
        await summarize_and_route_bid_document(
            parsed_file("bid-1"),
            audit_context().rule_set,
            SequenceModel([RuntimeError("402")]),
            temp_settings.bid_summary_model,
        )
    with pytest.raises(ValueError, match="修复失败"):
        await summarize_and_route_bid_document(
            parsed_file("bid-1"),
            audit_context().rule_set,
            SequenceModel(["坏JSON", "还是坏的"]),
            temp_settings.bid_summary_model,
        )


def test_evidence_catalog_keeps_file_level_entries(temp_settings):
    document = DocumentRef(
        file_id="file-1",
        file_name="合同.md",
        source_path="合同.md",
        data_role="evidence_material",
        parse_status="parsed",
        project_id=None,
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
