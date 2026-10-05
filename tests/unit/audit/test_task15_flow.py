"""投标摘要路由和文件级资料目录回归测试。"""

import pytest

from app.audit.bid_summary import summarize_and_route_bid_document
from app.audit.context import brief_catalog
from app.evidence.storage import load_library, write_library
from app.models.evidence import (
    EvidenceLibrary,
    EvidenceMaterial,
    EvidenceSourceFile,
)


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
    library = EvidenceLibrary(
        bidder_id="bidder-1",
        source_files=[
            EvidenceSourceFile(
                file_id="file-1", file_name="合同.md", source_path="合同.md"
            ),
            EvidenceSourceFile(
                file_id="file-2", file_name="验收.md", source_path="验收.md"
            ),
        ],
        materials=[
            EvidenceMaterial(
                file_id="file-1",
                bid_summary="红外设备供货合同，可用于核验业绩",
                evidence_summary="合同主体和标的物清晰可读",
                relations=[],
            ),
            EvidenceMaterial(
                file_id="file-2",
                bid_summary="设备验收记录，可用于核验履约",
                evidence_summary="验收数量和日期的详细记录",
                relations=[("对应供货合同", "file-1")],
            ),
        ],
    )
    write_library(library, temp_settings)
    loaded = load_library(library.bidder_id, temp_settings)
    assert loaded == library
    catalog = brief_catalog(loaded)
    assert catalog == (
        "### file-1\n\n红外设备供货合同，可用于核验业绩"
        "\n\n### file-2\n\n设备验收记录，可用于核验履约"
    )
    assert all(item.evidence_summary not in catalog for item in loaded.materials)
    assert brief_catalog(EvidenceLibrary(bidder_id="bidder-1")) == "无资料"
