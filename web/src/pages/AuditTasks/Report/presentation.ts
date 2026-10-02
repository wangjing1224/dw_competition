import type {
  Finding,
  ReportData,
  ReportHighlight,
} from '@/services/auditTasks';

// 与正式 MD/Word 一致：按完整冻结顺序关联，只精确去重建议并保留所有关联。
export function highlightDetails(data: ReportData, highlight: ReportHighlight) {
  const byId = new Map(data.result?.findings.map((f) => [f.rule_id, f]));
  const findings: Finding[] = [];
  const suggestions = new Map<string, string[]>();
  data.rules.forEach((rule) => {
    const finding = byId.get(rule.rule_id);
    if (!finding || !highlight.rule_ids.includes(rule.rule_id)) return;
    findings.push(finding);
    const text = finding.recommendation?.trim();
    if (text)
      suggestions.set(text, [
        ...(suggestions.get(text) || []),
        finding.rule_id,
      ]);
  });
  return {
    findings,
    conclusions: [...new Set(findings.map((f) => f.result))],
    suggestions: [...suggestions].map(([text, ruleIds]) => ({ text, ruleIds })),
  };
}
