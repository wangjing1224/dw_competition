import { requestJSON, downloadFile } from './libraryClient';
import type { Preview, Source, TraceNode, Warning } from './ruleLibraries';
export { ApiError } from './libraryClient';
export { fileSize, dateText } from './ruleLibraries';
export type { Preview, TraceNode };

export const api = <T>(path: string, init: RequestInit = {}) =>
  requestJSON<T>(`/api/bidders${path}`, init);
export const download = (path: string, filename: string) =>
  downloadFile(`/api/bidders${path}`, filename);
export const processing = (status: string) =>
  ['uploading_evidence', 'processing_evidence'].includes(status);
export const phases: Record<string, string> = {
  received: '材料已接收',
  parsing: '文档解析',
  indexing: '原文索引',
  ingesting: '资料整理',
  completed: '处理完成',
  failed: '处理失败',
};
export type Material = {
  file_id: string;
  provenance: 'ai_generated' | 'human_modified';
  bid_summary: string;
  evidence_summary: string;
  relations: [string, string][];
};
export type FileView = Source & {
  extension: string;
  size_bytes: number;
  parse_status: string;
  ingestion_status: string;
  has_material: boolean;
  has_reading_limit: boolean | null;
  provenance: Material['provenance'] | null;
};
export type Bidder = {
  bidder_id: string;
  display_name: string;
  status: string;
  created_at: string;
  updated_at: string;
};
export type Detail = {
  bidder: Bidder;
  library: {
    bidder_id: string;
    source_files: Source[];
    materials: Material[];
  } | null;
  files: FileView[];
  snapshot_kind: string;
  revision: string | null;
  editable: boolean;
  exportable: boolean;
  available_for_audit: boolean;
  warning: Warning;
  error: Warning;
};
export type LibraryItem = Bidder & {
  file_count: number;
  material_count: number | null;
  available_for_audit: boolean;
  has_reading_limit: boolean | null;
  warning: Warning;
};
export type ListResult = {
  items: LibraryItem[];
  total: number;
  page: number;
  page_size: number;
  overview: {
    library_count: number;
    ready_count: number;
    processing_count: number;
    attention_count: number;
  };
};
export type Creation = {
  bidder_id: string;
  status: string;
  client_request_id: string;
  reused: boolean;
};
export type ProgressData = {
  can_resume?: boolean;
  bidder_id: string;
  display_name: string;
  status: string;
  phase: string;
  failed_phase: string | null;
  document_count: number;
  current_file_id: string | null;
  completed_file_ids: string[];
  saved_material_count: number | null;
  recent_materials: Pick<Material, 'file_id' | 'bid_summary'>[];
  files: FileView[];
  error: Warning;
  warning: Warning;
  trace: {
    trace_status: string;
    changes: { seq: number; node: TraceNode }[];
    next_cursor: number;
    has_more: boolean;
    warning: string | null;
  } | null;
};
export type NodeDetail = {
  node: TraceNode;
  input: unknown;
  output: unknown;
  material_change: {
    file_id: string;
    before: Material | null;
    after: Material;
  } | null;
  error: Warning;
  truncated: boolean;
};
export function listLibraries(
  params: Record<string, string | number | boolean | undefined>,
  signal?: AbortSignal,
) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== '') query.set(key, String(value));
  });
  return api<ListResult>(`?${query}`, { signal });
}
export function editLibrary(
  id: string,
  revision: string,
  action: string,
  data: unknown,
) {
  return api<{ revision: string; changed: boolean; warning: string | null }>(
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
