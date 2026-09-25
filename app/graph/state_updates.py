"""成功保存的 Markdown 解析结果转成文件结果；不从聊天散文推断来源。"""

from app.models.findings import Finding
from app.models.results import FileAuditResult, ProjectMatch


def file_result_from_report(document, route, decision, ledger):
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
            for values in data.values():
                used.update(values)
    statuses = {str(item.result) for item in findings}
    conclusion = next(
        (key for key in ("non_compliant", "risk", "manual_review") if key in statuses),
        "compliant",
    )
    return FileAuditResult(
        file=document,
        is_applicable=True,
        project_match=ProjectMatch(
            status=route.match_status, reason=route.match_reason
        ),
        overall_conclusion=conclusion,
        audit_summary=decision.summary,
        category_summaries={
            str(group.category): group.summary for group in decision.categories
        },
        findings=findings,
        sources={key: ledger.validate(key) for key in used},
    )
