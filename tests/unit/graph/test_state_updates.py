"""MD 校验后的结果保留规则级结论与实际来源，不兜底伪造来源。"""

import pytest

from app.audit.sources import SourceLedger
from app.graph.state_updates import file_result_from_report
from app.reports.markdown_report import parse_report
from tests.helpers.audit import audit_context, filled_report


def test_result_uses_current_file_and_no_fabricated_sources():
    context = audit_context()
    ledger = SourceLedger(context.parsed_documents)
    decision, missing = parse_report(
        filled_report(
            context.rule_set, conclusion="不符合", title="采购项目技术投标审核报告"
        ),
        context.rule_set,
        ledger,
        final=True,
    )
    result = file_result_from_report(
        context.bid_files[0], context.bid_summaries["bid-1"], decision, ledger
    )
    assert not missing
    assert result.file.file_id == "bid-1"
    assert result.report_title == "采购项目技术投标审核报告"
    assert result.overall_conclusion == "non_compliant"
    assert not result.sources
    assert result.findings[0].rule_id == "rule-1"


def test_report_rejects_unread_source():
    context = audit_context()
    with pytest.raises(ValueError):
        parse_report(
            filled_report(context.rule_set) + "投标片段：chunk-fake\n",
            context.rule_set,
            SourceLedger(context.parsed_documents),
            final=True,
        )
