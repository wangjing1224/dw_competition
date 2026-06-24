"""审核规则覆盖和 chunk ID 结构公共断言。"""

from app.models.results import AuditResult, FileAuditResult
from app.models.rules import RuleSet


def assert_rule_coverage(
    rule_set: RuleSet,
    file_result: FileAuditResult,
) -> None:
    if not file_result.is_applicable:
        return
    expected = {rule.rule_id for rule in rule_set.rules}
    actual = {finding.rule_id for finding in file_result.findings}
    assert actual == expected


def assert_all_sources_traceable(result: AuditResult) -> None:
    known_rules = {rule.rule_id for rule in result.rules}
    for file_result in result.file_results:
        for finding in file_result.findings:
            assert finding.rule_id in known_rules
            for chunk_ids in (
                finding.tender_chunk_ids,
                finding.bid_chunk_ids,
                finding.evidence_chunk_ids,
            ):
                assert len(chunk_ids) == len(set(chunk_ids))
                assert all(chunk_id.startswith("chunk-") for chunk_id in chunk_ids)
