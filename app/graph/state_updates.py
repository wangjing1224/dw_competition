"""成功保存的 Markdown 解析结果转成文件结果；不从聊天散文推断来源。"""

from datetime import UTC, datetime

from app.models.findings import Finding
from app.models.results import FileAuditResult, ProjectMatch


def file_result_from_report(
    document,
    route,
    decision,
    ledger,
    *,
    version=1,
    report_context=None,
    rule_order=None,
):
    findings = []
    used = set()
    for category in decision.categories:
        for item in category.rules:
            data = item.summary.model_dump()
            reason = data.pop("reason")
            findings.append(
                Finding(
                    rule_id=item.rule_id, result=item.result, summary=reason, **data
                )
            )
            for field in (
                "tender_chunk_ids",
                "bid_chunk_ids",
                "evidence_chunk_ids",
                "image_paths",
            ):
                used.update(data[field])
    if rule_order is not None:
        order = {key: i for i, key in enumerate(rule_order)}
        findings.sort(key=lambda f: order[f.rule_id])
    if not findings:
        raise ValueError("规则审核结果为空，不能生成总体结论")
    statuses = {str(item.result) for item in findings}
    conclusion = next(
        (key for key in ("non_compliant", "manual_review") if key in statuses),
        "compliant",
    )
    return FileAuditResult(
        file=document,
        report_format_version=version,
        report_context=(
            report_context.model_copy(
                update={
                    "finalized_at": report_context.finalized_at or datetime.now(UTC)
                }
            )
            if report_context
            else None
        ),
        highlights=decision.highlights,
        report_title=decision.report_title,
        is_applicable=True,
        project_match=ProjectMatch(
            status=route.match_status, reason=route.match_reason
        ),
        overall_conclusion=conclusion,
        audit_summary=decision.summary,
        category_summaries={
            str(group.category): group.summary
            for group in decision.categories
            if group.summary
        },
        findings=findings,
        suggestions=list(
            dict.fromkeys(
                f.recommendation.strip()
                for f in findings
                if f.recommendation and f.recommendation.strip()
            )
        ),
        sources={key: ledger.validate(key) for key in used},
    )
