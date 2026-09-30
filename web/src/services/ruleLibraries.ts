import { requestJSON, downloadFile } from './libraryClient';
export { ApiError } from './libraryClient';
export const api = <T,>(path: string, init: RequestInit = {}) => requestJSON<T>(`/api/projects${path}`, init);
export const categories: Record<string, string> = {
  format: '格式与文件组成',
  qualification: '主体资格与准入',
  commercial: '商务响应',
  technical: '技术响应',
  performance_capacity: '业绩与能力',
  consistency_risk: '一致性与风险',
};
export const priorities: Record<string, string> = {
  critical: '关键',
  important: '重要',
  normal: '普通',
};
export const verifications: Record<string, string> = {
  bid_only: '投标文件内核验',
  evidence_check: '投标方资料核验',
};
export const provenances: Record<string, string> = {
  ai_generated: 'AI 整理',
  human_added: '人工新增',
  human_modified: '人工修订',
};
export const phases: Record<string, string> = {
  received: '材料已接收',
  parsing: '文档解析',
  indexing: '原文索引',
  extracting: '规则抽取',
  saving: '保存规则库',
  completed: '处理完成',
  failed: '处理失败',
};
export const statuses: Record<string, string> = {
  processing_rules: '处理中',
  rule_ready: '已完成',
  rule_failed: '失败',
};
export type Source = {
  file_id: string;
  file_name: string;
  source_path: string;
};
export type Rule = {
  rule_id: string;
  category: string;
  priority: string;
  verification: string;
  provenance: string;
  requirement: string;
  reason: string;
  sources: Source[];
};
export type SourceFile = Source & {
  extension: string;
  size_bytes: number;
  parse_status: string;
  preview_available: boolean;
  extraction_status?: string;
};
export type Warning = { code: string; message: string } | null;
export type Project = {
  project_id: string;
  display_name: string;
  status: string;
  created_at: string;
  updated_at: string;
  error?: Warning;
};
export type Detail = {
  project: Project;
  snapshot_kind: string;
  revision: string | null;
  available_for_audit: boolean;
  warning: Warning;
  rule_set: {
    project_id: string;
    project_profile: string;
    rules: Rule[];
    source_files: Source[];
  } | null;
  files: SourceFile[];
};
export type LibraryItem = Project & {
  file_count: number;
  rule_count: number | null;
  category_counts: Record<string, number> | null;
  warning: Warning;
  available_for_audit: boolean;
  snapshot_kind: string;
  phase: string;
};
export type ListResult = {
  items: LibraryItem[];
  total: number;
  page: number;
  page_size: number;
  overview: {
    project_count: number;
    ready_count: number;
    processing_count: number;
    failed_count: number;
    attention_count: number;
  };
};
export type TraceNode = {
  span_id: string;
  parent_span_id: string | null;
  kind: string;
  file_id: string | null;
  batch_index: number | null;
  turn_index: number | null;
  name: string;
  title: string;
  tool_call_id: string | null;
  status: string;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  summary: string;
  has_detail: boolean;
};
export type NodeDetail = {
  project_id: string;
  seq: number;
  node: TraceNode;
  input: Record<string, unknown> | null;
  output: Record<string, unknown> | null;
  error: Warning;
  truncated: boolean;
};
export type ProgressData = {
  project_id: string;
  display_name: string;
  status: string;
  phase: string;
  failed_phase: string | null;
  document_count: number;
  completed_file_ids: string[];
  saved_rule_count: number;
  files: SourceFile[];
  recent_rules: Rule[];
  // 与最近保存的规则来自同一完整快照；不能用 recent_rules 的位置编号。
  rule_order: string[];
  error: Warning;
  trace: {
    trace_status: string;
    changes: { seq: number; node: TraceNode }[];
    next_cursor: number;
    has_more: boolean;
    warning: string | null;
  } | null;
};
export type Preview = Source & {
  parse_status: string;
  parse_version: string;
  markdown: string;
  warnings: string[];
  images: {
    path: string;
    url: string;
    purpose: string;
    read_status: string;
    description: string;
  }[];
};
export type Creation = {
  project_id: string;
  status: string;
  display_name: string;
  file_count: number;
  client_request_id: string;
};
export function listLibraries(
  params: Record<string, string | number | boolean | undefined>,
  signal?: AbortSignal,
) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== '') query.set(k, String(v));
  });
  return api<ListResult>(`?${query}`, { signal });
}
export function editLibrary(
  id: string,
  revision: string,
  action: string,
  data: unknown,
) {
  return api<{ revision: string; rule?: Rule; project_profile?: string }>(
    `/${id}/edit`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'If-Match': `"${revision}"`,
      },
      body: JSON.stringify({ action, data }),
    },
  );
}
export const download = (path: string, filename: string) => downloadFile(`/api/projects${path}`, filename);
export const fileSize = (bytes: number) =>
  bytes < 1024 * 1024
    ? `${(bytes / 1024).toFixed(1)} KiB`
    : `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
export const dateText = (value: string) =>
  new Date(value).toLocaleString('zh-CN', { hour12: false });
