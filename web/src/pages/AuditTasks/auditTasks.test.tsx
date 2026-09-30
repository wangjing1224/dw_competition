// @vitest-environment jsdom
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { App } from 'antd';
import { useState } from 'react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { mergeTrace, orderedFindings } from '@/services/auditTasks';
import type {
  AuditFile,
  Batch,
  Finding,
  ReportData,
  Span,
} from '@/services/auditTasks';
import type { Rule } from '@/services/ruleLibraries';
import { safeMarkdown, validImageUrl } from '@/components/SourcePreview';
import List from './List';
import Create from './Create';
import Report from './Report';
import Progress from './Progress';

vi.mock('@umijs/max', () => ({
  Link: ({ to, children, ...props }: { to: string; children: ReactNode }) => (
    <a href={to} {...props}>
      {children}
    </a>
  ),
  history: { push: vi.fn() },
  useParams: () => ({ taskId: 'audit-1' }),
  useSearchParams: () => useState(new URLSearchParams('file=file-1')),
}));
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
});
const file: AuditFile = {
  file_id: 'file-1',
  file_name: '标书一.md',
  source_path: '甲/标书一.md',
  status: 'completed',
  phase: 'finished',
  project_id: 'project-1',
  project_name: '规则库甲',
  bidder_id: 'bidder-1',
  bidder_name: '资料甲',
  stages: ['parsing', 'routing', 'indexing', 'auditing', 'exporting'].map(
    (phase) => ({ phase, status: 'completed', reason: null }),
  ),
  route: { match_status: 'matched', match_reason: '项目一致' },
  word_status: 'ready',
  error: null,
  export_error: null,
  report_kind: 'final',
  report_revision: 'rev1',
  overall_conclusion: 'risk',
  outputs: {
    json_url: '/api/audit-tasks/audit-1/outputs?file_id=file-1&format=json',
    md_url: null,
    word_url: null,
  },
};
const task: Batch = {
  task_id: 'audit-1',
  display_name: '联合审核',
  status: 'completed',
  total_files: 1,
  finished_files: 1,
  completed_files: 1,
  failed_files: 0,
  running_files: 0,
  queued_files: 0,
  project_count: 1,
  bidder_count: 1,
  created_at: '2026-09-28T12:00:00Z',
  updated_at: '2026-09-28T12:01:00Z',
  completed_at: '2026-09-28T12:01:00Z',
  legacy: false,
  error: null,
  outputs: { batch_json_url: null },
};
const rules: Rule[] = ['rule-1', 'rule-2'].map((rule_id, i) => ({
  rule_id,
  category: 'technical',
  priority: i ? 'normal' : 'critical',
  verification: 'bid_only',
  provenance: 'ai_generated',
  requirement: `要求${i + 1}`,
  reason: '技术要求',
  sources: [
    { file_id: 'file-3', file_name: '招标原文.md', source_path: '招标原文.md' },
  ],
}));
const finding = (rule_id: string, result: string): Finding => ({
  rule_id,
  result,
  summary: `${rule_id}的核验说明`,
  bid_chunk_ids: [],
  tender_chunk_ids: [],
  evidence_chunk_ids: [],
  image_paths: [],
});
const report: ReportData = {
  file,
  document: { kind: 'final', markdown: '# 审核报告\n\n完成', revision: 'rev1' },
  rules,
  counts: {
    total: 2,
    compliant: 1,
    non_compliant: 0,
    risk: 1,
    manual_review: 0,
  },
  result: {
    is_applicable: true,
    overall_conclusion: 'risk',
    audit_summary: '精度待核对',
    project_match: { status: 'matched', reason: '项目一致' },
    exclusion_reason: null,
    category_summaries: { technical: '核对精度' },
    findings: [finding('rule-1', 'risk'), finding('rule-2', 'compliant')],
    suggestions: [],
    sources: {},
  },
  source_links: [],
};
const span = (id: string, status = 'completed'): Span => ({
  span_id: id,
  parent_span_id: null,
  kind: 'model',
  actor: 'bid_agent',
  title: '模型调用',
  name: 'model',
  file_id: 'file-1',
  batch_index: 1,
  turn_index: 1,
  tool_call_id: null,
  status,
  started_at: '2026-09-28T12:00:00Z',
  finished_at: '2026-09-28T12:01:00Z',
  duration_ms: 1000,
  summary: '完成',
  has_detail: true,
});
function mockApi(overrides?: (url: string) => unknown) {
  const fetch = vi.fn(async (input: string) => {
    const path = String(input);
    const custom = overrides?.(path);
    let body: unknown = custom;
    if (custom === undefined) {
      if (path.includes('/progress'))
        body = {
          task,
          files: [file],
          activity: {
            model_calls: 3,
            tool_calls: 1,
            evidence_checks: 0,
            report_saves: 1,
            observation: 'complete',
          },
          trace_updates: [{ seq: 1, span: span('span-1') }],
          next_seq: 1,
          has_more: false,
          trace_warning: null,
        };
      else if (path === '/api/audit-tasks/audit-1/files/file-1') body = report;
      else if (path === '/api/audit-tasks/audit-1')
        body = { task, files: [file] };
      else if (path.includes('/trace/'))
        body = {
          span: span('span-1'),
          input: { role: 'bid_agent' },
          output: '已调用工具',
          error: null,
          truncated: false,
        };
      else
        body = {
          items: [task],
          total: 1,
          page: 1,
          page_size: 10,
          overview: { total: 1, running: 0, completed: 1, attention: 0 },
        };
    }
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fetch);
  return fetch;
}
describe('审核工作区', () => {
  it('报告编号来自冻结规则快照，按结论筛选不会重编号，也不读取当前规则库', async () => {
    const frozenRules = rules.map((r, i) => ({
      ...r,
      rule_id: `rule-${147 + i}`,
    }));
    const fetch = mockApi((url) =>
      url.endsWith('/files/file-1')
        ? {
            ...report,
            rules: frozenRules,
            result: {
              ...report.result,
              findings: [
                finding('rule-147', 'compliant'),
                finding('rule-148', 'risk'),
              ],
            },
          }
        : undefined,
    );
    render(
      <App>
        <Report />
      </App>,
    );
    await screen.findByRole('button', { name: '规则 02' });
    expect(
      screen.queryByRole('button', { name: '规则 01' }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText('rule-148')).not.toBeInTheDocument();
    for (const id of ['project-1', 'bidder-1', 'audit-1', 'file-1', 'file-3']) {
      expect(screen.queryByText(id)).not.toBeInTheDocument();
    }
    expect(screen.getByText('规则库 · 规则库甲')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '查看全部规则' }));
    expect(
      await screen.findByRole('button', { name: '规则 01' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '规则 02' })).toBeInTheDocument();
    expect(
      fetch.mock.calls.every(([url]) => !url.startsWith('/api/projects')),
    ).toBe(true);
  });
  it('span 完成覆盖开始，旧更新不得覆盖新更新', () => {
    const merged = mergeTrace(
      [{ seq: 2, span: span('s') }],
      [
        { seq: 1, span: span('s', 'running') },
        { seq: 3, span: span('t') },
      ],
    );
    expect(merged).toHaveLength(2);
    expect(merged[0].span.status).toBe('completed');
  });
  it('问题清单按结论、重要程度、规则快照顺序排列', () => {
    expect(
      orderedFindings(
        [finding('rule-2', 'compliant'), finding('rule-1', 'risk')],
        rules,
      ).map((f) => f.rule_id),
    ).toEqual(['rule-1', 'rule-2']);
  });
  it('审核图片仅允许已绑定的目标、来源和解析版本', () => {
    const version = 'a'.repeat(64);
    const base = '/api/audit-tasks/audit-1/files/file-1/sources/file-3/preview';
    const image = `${base}?image_name=1.png&version=${version}`;
    expect(
      validImageUrl(image, 'file-3/images/1.png', { baseUrl: base, version }),
    ).toBe(true);
    expect(
      validImageUrl(
        image.replace('audit-1', 'audit-2'),
        'file-3/images/1.png',
        { baseUrl: base, version },
      ),
    ).toBe(false);
    expect(
      validImageUrl(image, 'file-4/images/1.png', { baseUrl: base, version }),
    ).toBe(false);
    expect(
      safeMarkdown('<script>alert(1)</script>![x](https://evil.test/x)', []),
    ).not.toContain('<script');
    expect(safeMarkdown('![x](https://evil.test/x)', [])).not.toContain('<img');
  });
  it('总览使用任务总数，筛选不改变全量指标', async () => {
    mockApi();
    render(
      <App>
        <List />
      </App>,
    );
    expect(await screen.findByText('联合审核')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /任务总数/ }),
    ).toBeInTheDocument();
    expect(screen.getByText('文件处理矩阵')).toBeInTheDocument();
    expect(
      await screen.findByText('标书一.md', { selector: 'strong' }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: '导出任务结果' }),
    ).toBeInTheDocument();
  });
  it('文件未配置两库时禁止提交；不为每行提前请求所有库', async () => {
    const fetch = mockApi();
    const { container } = render(
      <App>
        <Create />
      </App>,
    );
    const input = container.querySelector('input[type=file]');
    if (!input) throw new Error('文件选择入口不存在');
    fireEvent.change(input, {
      target: { files: [new File(['content'], 'a.md')] },
    });
    expect(
      await screen.findByText('a.md', { selector: 'strong' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '开始审核' })).toBeDisabled();
    expect(fetch).not.toHaveBeenCalled();
  });
  it('刷新后未确认提交只查询，不自动再次 POST', async () => {
    sessionStorage.setItem('audit-pending-submission', 'old-request');
    const fetch = mockApi(() => ({ items: [], request_state: 'receiving' }));
    render(
      <App>
        <Create />
      </App>,
    );
    expect(screen.getByText('存在待确认的提交')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '查询提交状态' }));
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(
      fetch.mock.calls.every(([url]) =>
        url.includes('client_request_id=old-request'),
      ),
    ).toBe(true);
  });
  it('正式报告图表使用全量 counts，没有虚构精确证据对照', async () => {
    mockApi();
    render(
      <App>
        <Report />
      </App>,
    );
    expect(
      await screen.findByRole('img', {
        name: '符合 1 条，不符合 0 条，风险 1 条，人工复核 0 条',
      }),
    ).toBeInTheDocument();
    expect(screen.queryByText('证据对照')).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: '导出任务结果' }),
    ).not.toBeInTheDocument();
    for (const name of ['下载 Word', '导出 Markdown', '导出 JSON']) {
      expect(screen.getByRole('button', { name })).toBeInTheDocument();
    }
    expect(
      screen.getByRole('button', { name: '查看解析原文' }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '查看全部规则' }));
    expect(await screen.findByText('rule-2的核验说明')).toBeInTheDocument();
    expect(
      screen.getByRole('img', {
        name: '符合 1 条，不符合 0 条，风险 1 条，人工复核 0 条',
      }),
    ).toBeInTheDocument();
  });
  it('路由排除文件不显示正式结论图', async () => {
    mockApi((url) =>
      url.endsWith('/files/file-1')
        ? {
            ...report,
            counts: null,
            result: {
              ...report.result,
              is_applicable: false,
              overall_conclusion: 'not_applicable',
              exclusion_reason: '不是本次项目',
              findings: [],
            },
          }
        : undefined,
    );
    render(
      <App>
        <Report />
      </App>,
    );
    expect(await screen.findByText('不是本次项目')).toBeInTheDocument();
    expect(screen.queryByText('已核验规则')).not.toBeInTheDocument();
  });
  it('进度使用真实服务端活动统计，不把树节点数当模型调用数', async () => {
    mockApi();
    render(
      <App>
        <Progress />
      </App>,
    );
    expect(
      await screen.findByText(/当前文件已观测调用次数/),
    ).toBeInTheDocument();
    const band = document.querySelector('.audit-band');
    expect(band?.textContent).toContain('模型调用3');
    expect(screen.getByText('事件流水 · 1 个已观测节点')).toBeInTheDocument();
  });
});
