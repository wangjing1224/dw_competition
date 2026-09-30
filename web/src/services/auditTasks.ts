import { requestJSON, downloadFile } from './libraryClient';
import type { Rule, TraceNode } from './ruleLibraries';
export { downloadFile };
export const api = <T,>(path: string, init: RequestInit = {}) => requestJSON<T>(`/api/audit-tasks${path}`, init);
export const statusLabels: Record<string, string> = { queued: '排队中', running: '执行中', completed: '已完成', failed: '执行失败', partially_failed: '部分失败', completed_with_report_error: '导出异常' };
export const stageLabels: Record<string, string> = { parsing: '解析', routing: '匹配', indexing: '索引', auditing: '审核', exporting: '导出' };
export const stageStates: Record<string, string> = { waiting: '等待', running: '进行中', completed: '完成', skipped: '跳过', failed: '失败', not_run: '未执行', interrupted: '中断' };
export const resultLabels: Record<string, string> = { compliant: '符合', non_compliant: '不符合', risk: '风险', manual_review: '人工复核', not_applicable: '不适用' };
export const resultColors: Record<string, string> = { compliant: '#16a085', non_compliant: '#e45858', risk: '#f4a340', manual_review: '#7984d5' };
export type Problem = { code: string; message: string } | null;
export type Batch = { task_id: string; display_name: string; status: string; total_files: number; finished_files: number; completed_files: number; failed_files: number; running_files: number; queued_files: number; project_count: number; bidder_count: number; created_at: string; updated_at: string; completed_at: string | null; legacy: boolean; error: Problem; outputs: { batch_json_url: string | null } };
export type AuditFile = { file_id: string; file_name: string; source_path: string; status: string; phase: string; project_id: string; project_name: string; bidder_id: string; bidder_name: string; stages: { phase: string; status: string; reason: string | null }[]; route: { match_status: string; match_reason: string } | null; word_status: string; error: Problem; export_error: Problem; report_kind: 'none' | 'draft' | 'final'; report_revision: string | null; overall_conclusion: string | null; outputs: { json_url: string | null; md_url: string | null; word_url: string | null } };
export type TaskDetail = { task: Batch; files: AuditFile[] };
export type TaskList = { items: Batch[]; total: number; page: number; page_size: number; overview: { total: number; running: number; completed: number; attention: number }; request_state: 'not_found' | 'receiving' | 'accepted' | 'failed' | null; request_error: Problem };
export type Span = TraceNode & { task_id?: string; actor: string; step?: number | null; session_id?: string | null; rule_id?: string | null; check_index?: number | null; target_file_ids?: string[]; tool_name?: string | null; ended_at?: string | null };
export type TraceUpdate = { seq: number; span: Span };
export type ProgressData = TaskDetail & { activity: { scope?: 'file' | 'batch'; file_id?: string | null; model_calls: number | null; tool_calls: number | null; evidence_checks: number | null; report_saves: number | null; observation: string }; trace_updates: TraceUpdate[]; next_seq: number; has_more: boolean; trace_warning: Problem };
export type SpanDetail = { span: Span; input: unknown; output: unknown; error: Problem; truncated: boolean };
export type Finding = { rule_id: string; result: string; summary: string; tender_chunk_ids: string[]; bid_chunk_ids: string[]; evidence_chunk_ids: string[]; image_paths: string[] };
export type Source = { file_id: string; file_name: string; source_path: string; data_role: string; parse_version: string; section: string | null; page_numbers: number[]; quote: string };
export type ReportData = { file: AuditFile; document: { kind: string; markdown: string | null; revision: string | null }; result: { is_applicable: boolean; overall_conclusion: string; audit_summary: string | null; project_match: { status: string; reason: string }; exclusion_reason: string | null; category_summaries: Record<string, string>; findings: Finding[]; suggestions: string[]; sources: Record<string, Source> } | null; rules: Rule[]; counts: Record<string, number> | null; source_links: { source_key: string; file_id: string; preview_url: string; download_url: string }[] };
export function mergeTrace(previous: TraceUpdate[], incoming: TraceUpdate[]) {
  const map = new Map(previous.map(v => [v.span.span_id, v]));
  incoming.forEach(v => { if (v.seq > (map.get(v.span.span_id)?.seq ?? -1)) map.set(v.span.span_id, v); });
  return [...map.values()].sort((a,b) => a.span.started_at.localeCompare(b.span.started_at) || a.seq-b.seq);
}
export function orderedFindings(findings: Finding[], rules: Rule[]) {
  const resultOrder = ['non_compliant', 'risk', 'manual_review', 'compliant'];
  const priorityOrder = ['critical', 'important', 'normal'];
  const index = new Map(rules.map((rule,i) => [rule.rule_id, {rule,i}]));
  return [...findings].sort((a,b) => resultOrder.indexOf(a.result)-resultOrder.indexOf(b.result) || priorityOrder.indexOf(index.get(a.rule_id)?.rule.priority || 'normal')-priorityOrder.indexOf(index.get(b.rule_id)?.rule.priority || 'normal') || (index.get(a.rule_id)?.i ?? 0)-(index.get(b.rule_id)?.i ?? 0));
}
export const fileUrl = (task: string, file: string, page = 'report') => `/audit-tasks/${task}/${page}?file=${encodeURIComponent(file)}`;
