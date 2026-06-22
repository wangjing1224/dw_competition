"""审核规则覆盖、来源完整性和结果一致性公共断言。"""

from app.graph.audit_state import applicable_rules_for_file
from app.models.results import AuditResult, FileAuditResult
from app.models.rules import RuleSet


def assert_rule_coverage(
    rule_set: RuleSet, file_result: FileAuditResult
) -> None:
    if not file_result.is_applicable:
        return
    expected = {
        rule.rule_id
        for rule in applicable_rules_for_file(rule_set, file_result.file)
    }
    actual = {finding.rule_id for finding in file_result.findings}
    assert actual == expected


def assert_all_sources_traceable(result: AuditResult) -> None:
    for file_result in result.file_results:
        for finding in file_result.findings:
            assert finding.rule_source.file_id
            assert finding.rule_source.source_path
            if finding.response_status == "found":
                assert finding.bid_source is not None
        for review in file_result.evidence_results:
            for source in review.evidence_found:
                assert source.file_id
                assert source.source_path
