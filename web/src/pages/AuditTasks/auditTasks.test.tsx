// @vitest-environment jsdom
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { App, Grid } from 'antd';
import type { ReactNode } from 'react';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { safeMarkdown, validImageUrl } from '@/components/SourcePreview';
import type {
  AuditFile,
  Batch,
  Finding,
  ProgressData,
  ReportData,
  Span,
} from '@/services/auditTasks';
import { mergeTrace, orderedFindings } from '@/services/auditTasks';
import type { Rule } from '@/services/ruleLibraries';
import Create from './Create';
import List from './List';
import Progress from './Progress';
import Report from './Report';
import { StageMatrix } from './shared';

vi.mock('@umijs/max', () => ({
  Link: ({ to, children, ...props }: { to: string; children: ReactNode }) => (
    <a href={to} {...props}>
      {children}
    </a>
  ),
  history: { push: vi.fn() },
  useParams: () => ({ taskId: 'audit-1' }),
  useSearchParams: () => {
    const [params, setParams] = useState(new URLSearchParams('file=file-1'));
    return [
      params,
      (next: URLSearchParams | Record<string, string>) =>
        setParams(new URLSearchParams(next)),
    ];
  },
}));
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
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
function excluded(status = 'not_matched'): ReportData {
  if (!report.result) throw new Error('测试结果缺失');
  return {
    ...report,
    counts: null,
    file: {
      ...file,
      route: { match_status: status, match_reason: '不是本次项目' },
      stages: file.stages.map((s) =>
        ['indexing', 'auditing'].includes(s.phase)
          ? { ...s, status: 'skipped' }
          : s,
      ),
    },
    result: {
      ...report.result,
      is_applicable: false,
      overall_conclusion:
        status === 'uncertain' ? 'manual_review' : 'not_applicable',
      project_match: { status, reason: '不是本次项目' },
      exclusion_reason: '不是本次项目',
      findings: [],
    },
  };
}
function progressData(
  spans: Span[] = [span('span-1')],
  currentFile = file,
): ProgressData {
  return {
    task,
    files: [currentFile],
    activity: {
      scope: 'file',
      file_id: currentFile.file_id,
      model_calls: 3,
      tool_calls: 1,
      evidence_checks: 0,
      report_saves: 1,
      observation: 'complete',
    },
    trace_updates: spans.map((s, i) => ({ seq: i + 1, span: s })),
    next_seq: spans.length,
    has_more: false,
    trace_warning: null,
  };
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
    fireEvent.mouseDown(screen.getByRole('combobox', { name: '规则结论筛选' }));
    fireEvent.click(
      await screen.findByText('全部规则', {
        selector: '.ant-select-item-option-content',
      }),
    );
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
  it('文件矩阵整行可选，名称按钮保留键盘入口且点击只触发一次', () => {
    const onSelect = vi.fn();
    render(
      <StageMatrix
        files={[file]}
        selected={file.file_id}
        onSelect={onSelect}
      />,
    );
    const table = screen.getByRole('table', { name: '文件处理矩阵' });
    const row = within(table).getAllByRole('row')[1];
    expect(row).toHaveAttribute('aria-selected', 'true');
    const button = within(row).getByRole('button', { name: file.file_name });
    expect(button).toHaveAttribute('type', 'button');
    expect(button).toHaveAttribute('aria-pressed', 'true');
    button.focus();
    expect(button).toHaveFocus();
    for (const target of [
      row,
      within(row).getAllByRole('cell')[2],
      within(row).getByText(file.source_path),
      button,
    ]) {
      onSelect.mockClear();
      fireEvent.click(target);
      expect(onSelect).toHaveBeenCalledExactlyOnceWith(file);
    }
  });
  it('点击文件矩阵的状态区域切换文件详情及报告链接', async () => {
    const second = {
      ...file,
      file_id: 'file-2',
      file_name: '标书二.md',
      source_path: '乙/标书二.md',
    };
    mockApi((url) =>
      url.includes('/progress')
        ? {
            ...progressData(),
            task: { ...task, total_files: 2, finished_files: 2 },
            files: [file, second],
          }
        : undefined,
    );
    render(
      <App>
        <List />
      </App>,
    );
    const table = await screen.findByRole('table', { name: '文件处理矩阵' });
    const rows = within(table).getAllByRole('row');
    fireEvent.click(within(rows[2]).getAllByRole('cell')[3]);
    expect(rows[1]).toHaveAttribute('aria-selected', 'false');
    expect(rows[2]).toHaveAttribute('aria-selected', 'true');
    expect(
      screen.getByText(second.file_name, { selector: 'strong' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '查看报告' })).toHaveAttribute(
      'href',
      '/audit-tasks/audit-1/report?file=file-2',
    );
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
    expect(
      screen.queryByRole('button', { name: '查看全部规则' }),
    ).not.toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole('combobox', { name: '规则结论筛选' }));
    fireEvent.click(
      await screen.findByText('全部规则', {
        selector: '.ant-select-item-option-content',
      }),
    );
    expect(await screen.findByText('rule-2的核验说明')).toBeInTheDocument();
    expect(
      screen.getByRole('img', {
        name: '符合 1 条，不符合 0 条，风险 1 条，人工复核 0 条',
      }),
    ).toBeInTheDocument();
  });
  it('路由排除文件不显示正式结论图', async () => {
    mockApi((url) => (url.endsWith('/files/file-1') ? excluded() : undefined));
    render(
      <App>
        <Report />
      </App>,
    );
    expect(await screen.findByText('不是本次项目')).toBeInTheDocument();
    expect(screen.queryByText('已核验规则')).not.toBeInTheDocument();
  });
  it('报告长结论默认收起，规则详情分区渲染 Markdown 而非原始标记', async () => {
    const long = `**关键风险**\n\n${'依据当前文件核验。'.repeat(80)}`;
    mockApi((url) =>
      url.endsWith('/files/file-1')
        ? {
            ...report,
            result: {
              ...report.result,
              audit_summary: long,
              findings: [
                {
                  ...finding('rule-1', 'risk'),
                  summary: '**需要复核**\n\n- 查验原件',
                },
              ],
            },
          }
        : undefined,
    );
    const { container } = render(
      <App>
        <Report />
      </App>,
    );
    const toggle = await screen.findByRole('button', { name: '展开总体结论' });
    expect(screen.getByRole('region', { name: '总体结论' })).toHaveStyle({
      maxHeight: '168px',
    });
    fireEvent.click(toggle);
    expect(screen.getByRole('region', { name: '总体结论' })).toHaveStyle({
      overflowY: 'auto',
      maxHeight: '360px',
    });
    expect(
      container.querySelector('.audit-finding-explanation strong'),
    ).toHaveTextContent('需要复核');
    expect(
      container.querySelector('.audit-finding-explanation li'),
    ).toHaveTextContent('查验原件');
    expect(
      screen.queryByRole('button', { name: '查看全部规则' }),
    ).not.toBeInTheDocument();
  });
  it('轨迹长输入输出默认收起，在详情框内展开且不丢失原文', async () => {
    vi.spyOn(Grid, 'useBreakpoint').mockReturnValue({ lg: true });
    const input = {
      checks: Array.from({ length: 15 }, (_, i) => ({
        request: `核验事项 ${i} 及其对应依据`,
        file_ids: ['file-1'],
      })),
    };
    const output = `**核验结果**\n\n${'需要进一步核对来源。'.repeat(100)}`;
    mockApi((url) =>
      url.includes('/trace/')
        ? { span: span('span-1'), input, output, error: null, truncated: false }
        : undefined,
    );
    render(
      <App>
        <Progress />
      </App>,
    );
    await screen.findByText('当前文件 · 已观测');
    fireEvent.click(screen.getByRole('radio', { name: '轨迹树' }));
    fireEvent.click(
      screen.getByText('模型调用', { selector: '.ant-tree-title span' }),
    );
    const inputToggle = await screen.findByRole('button', {
      name: '展开调用输入',
    });
    expect(screen.getByRole('region', { name: '调用输入' })).toHaveStyle({
      maxHeight: '128px',
    });
    expect(
      screen.getByRole('button', { name: '展开可见输出' }),
    ).toBeInTheDocument();
    fireEvent.click(inputToggle);
    expect(screen.getByRole('region', { name: '调用输入' })).toHaveStyle({
      overflowY: 'auto',
      maxHeight: '360px',
    });
    expect(screen.getByRole('region', { name: '调用输入' })).toHaveTextContent(
      '核验事项 14',
    );
  });
  it('进度使用真实服务端活动统计，不把树节点数当模型调用数', async () => {
    mockApi();
    render(
      <App>
        <Progress />
      </App>,
    );
    expect(await screen.findByText('当前文件 · 已观测')).toBeInTheDocument();
    const band = document.querySelector('.audit-observed');
    expect(band?.textContent).toContain('模型调用3');
    expect(screen.queryByText(/事件流水/)).not.toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '执行记录' }));
    expect(await screen.findByText(/已加载 1 个独立节点/)).toBeInTheDocument();
  });
  it('缺失正式结果不被误判成不匹配', async () => {
    mockApi((url) =>
      url.endsWith('/files/file-1') ? { ...report, result: null } : undefined,
    );
    render(
      <App>
        <Report />
      </App>,
    );
    expect(
      await screen.findByText('正式结果缺失或状态不一致，无法确认审核结论'),
    ).toBeInTheDocument();
    expect(screen.queryByText('项目不匹配')).not.toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: '查看处理过程' }),
    ).toBeInTheDocument();
  });
  it('手动切换视图清空轨迹详情和高亮，标签跟随文件选择放在同一工具栏', async () => {
    vi.spyOn(Grid, 'useBreakpoint').mockReturnValue({ lg: true });
    const fetch = mockApi();
    render(
      <App>
        <Progress />
      </App>,
    );
    await screen.findByText('当前文件 · 已观测');
    const toolbar = screen
      .getByRole('combobox', { name: '当前投标文件' })
      .closest('.audit-context-toolbar');
    expect(toolbar?.textContent).toContain('规则库 · 规则库甲');
    expect(toolbar?.textContent).toContain('资料库 · 资料甲');
    expect(screen.getAllByText('规则库 · 规则库甲')).toHaveLength(1);
    fireEvent.click(screen.getByRole('radio', { name: '轨迹树' }));
    fireEvent.click(screen.getByText('模型调用'));
    expect(await screen.findByText('已调用工具')).toBeInTheDocument();
    expect(
      screen.getByRole('region', { name: '节点详情' }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('radio', { name: '流程图' }));
    expect(
      screen.queryByRole('region', { name: '节点详情' }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('radio', { name: 'Agent 图' }));
    expect(
      screen.queryByRole('region', { name: '节点详情' }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('radio', { name: '轨迹树' }));
    expect(
      screen.queryByRole('region', { name: '节点详情' }),
    ).not.toBeInTheDocument();
    expect(document.querySelector('.ant-tree-node-selected')).toBeNull();
    expect(
      fetch.mock.calls.filter(([url]) => url.includes('/progress')),
    ).toHaveLength(1);
  });
  it('执行记录顶部显示短日期，跨天切换日期，表格只保留时分秒', async () => {
    const days = [
      { ...span('old'), title: '昨日调用', started_at: '2026-09-29T10:30:00' },
      { ...span('new'), title: '今日调用', started_at: '2026-09-30T10:30:00' },
    ];
    mockApi((url) =>
      url.includes('/progress') ? progressData(days) : undefined,
    );
    render(
      <App>
        <Progress />
      </App>,
    );
    await screen.findByText('当前文件 · 已观测');
    fireEvent.click(screen.getByRole('button', { name: '执行记录' }));
    await screen.findByRole('combobox', { name: '记录日期' });
    const table = screen.getByRole('table');
    expect(table.textContent).toContain('今日调用');
    expect(table.textContent).not.toContain('昨日调用');
    expect(table.textContent).not.toContain('2026');
    expect(table.querySelector('time')?.textContent).toBe('10:30:00');
    fireEvent.mouseDown(screen.getByRole('combobox', { name: '记录日期' }));
    fireEvent.click(
      await screen.findByText('2026/09/29', {
        selector: '.ant-select-item-option-content',
      }),
    );
    await waitFor(() => expect(table.textContent).toContain('昨日调用'));
    expect(table.textContent).not.toContain('今日调用');
  });
  it('无法确定说明保留原始原因，Word失败不会禁用其他格式', async () => {
    const data = excluded('uncertain');
    data.file.word_status = 'failed';
    data.file.outputs = {
      ...file.outputs,
      md_url: '/api/audit-tasks/audit-1/outputs?format=md&file_id=file-1',
    };
    const fetch = mockApi((url) =>
      url.endsWith('/files/file-1') ? data : undefined,
    );
    render(
      <App>
        <Report />
      </App>,
    );
    expect(await screen.findByText('项目适用性需人工判断')).toBeInTheDocument();
    expect(screen.getByText('不是本次项目')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Word 导出失败' }),
    ).toBeDisabled();
    expect(screen.queryByText('项目不匹配')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /更多导出/ }));
    expect(
      await screen.findByRole('menuitem', { name: '导出 JSON' }),
    ).not.toHaveAttribute('aria-disabled', 'true');
    expect(
      screen.getByRole('menuitem', { name: '导出 Markdown' }),
    ).not.toHaveAttribute('aria-disabled', 'true');
    expect(fetch.mock.calls.every(([url]) => !url.includes('/projects'))).toBe(
      true,
    );
  });
  it('不匹配显示明确跳过，不生成假Agent或零值审核统计', async () => {
    const data = progressData(
      [{ ...span('route'), kind: 'routing', actor: 'router' }],
      excluded().file,
    );
    data.activity = {
      ...data.activity,
      model_calls: 1,
      tool_calls: 0,
      report_saves: 0,
    };
    const fetch = mockApi((url) =>
      url.includes('/progress') ? data : undefined,
    );
    render(
      <App>
        <Progress />
      </App>,
    );
    await screen.findByText('未启动审核 Agent');
    expect(screen.getAllByRole('button', { name: '查看报告' })).toHaveLength(1);
    fireEvent.click(screen.getByRole('radio', { name: 'Agent 图' }));
    expect(
      await screen.findByText('本文件未进入审核 Agent'),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('region', { name: 'Agent 执行图' }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText('报告保存')).not.toBeInTheDocument();
    const boundary = screen.getByRole('region', { name: '本次处理范围与输出' });
    expect(within(boundary).getByText('已跳过')).toBeInTheDocument();
    expect(within(boundary).getByText('未执行')).toBeInTheDocument();
    expect(
      within(boundary).getByText('正式结果已就绪 · Word 已生成'),
    ).toBeInTheDocument();
    expect(document.querySelector('.audit-skipped-agent aside')).toBeNull();
    expect(screen.getByRole('button', { name: '查看路由轨迹' })).toBeEnabled();
    fireEvent.click(screen.getByRole('radio', { name: '轨迹树' }));
    fireEvent.click(screen.getByRole('radio', { name: '流程图' }));
    expect(
      fetch.mock.calls.filter(([url]) => url.includes('/progress')),
    ).toHaveLength(1);
    expect(
      fetch.mock.calls.filter(([url]) => url === '/api/audit-tasks/audit-1'),
    ).toHaveLength(1);
  });
  it('未进入 Agent 的输出区保留待判断与导出失败的真实状态', async () => {
    const data = progressData([], {
      ...excluded('uncertain').file,
      word_status: 'failed',
    });
    mockApi((url) => (url.includes('/progress') ? data : undefined));
    render(
      <App>
        <Progress />
      </App>,
    );
    await screen.findByText('未启动审核 Agent');
    fireEvent.click(screen.getByRole('radio', { name: 'Agent 图' }));
    expect(
      await screen.findByText('本文件暂未进入审核 Agent'),
    ).toBeInTheDocument();
    const boundary = screen.getByRole('region', { name: '本次处理范围与输出' });
    expect(
      within(boundary).getByText('正式结果已就绪 · Word 导出失败'),
    ).toBeInTheDocument();
    expect(within(boundary).queryByText(/Word 已生成/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '查看路由轨迹' })).toBeDisabled();
    expect(
      screen.getByText('跳过规则审核不代表审核不通过。'),
    ).toBeInTheDocument();
  });
  it('Agent 回放控制可暂停、拖动、重播和退出；不新增请求，切视图不残留', async () => {
    const main = { ...span('main'), kind: 'batch', title: '主审核会话' };
    const model = {
      ...span('replay-model'),
      parent_span_id: 'main',
      started_at: '2026-09-28T12:00:01Z',
      finished_at: '2026-09-28T12:00:30Z',
      duration_ms: 29000,
    };
    const fetch = mockApi((url) =>
      url.includes('/progress') ? progressData([main, model]) : undefined,
    );
    render(
      <App>
        <Progress />
      </App>,
    );
    await screen.findByText('当前文件 · 已观测');
    fireEvent.click(screen.getByRole('radio', { name: 'Agent 图' }));
    const requests = fetch.mock.calls.length;
    fireEvent.click(screen.getByRole('button', { name: '回放执行' }));
    expect(screen.getByText('回放中')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '暂停回放' }));
    expect(screen.getByText('回放已暂停')).toBeInTheDocument();
    const slider = screen.getByRole('slider', { name: '回放进度' });
    fireEvent.keyDown(slider, { key: 'End', keyCode: 35, which: 35 });
    expect(slider).toHaveAttribute('aria-valuenow', '60000');
    expect(
      screen.getByRole('button', { name: '重新播放' }),
    ).toBeInTheDocument();
    const graph = screen.getByRole('region', { name: 'Agent 执行图' });
    expect(
      within(graph).getByRole('button', {
        name: /主审核 Agent.*模型调用 1 次/,
      }),
    ).toHaveClass('node-completed');
    fireEvent.keyDown(slider, { key: 'Home', keyCode: 36, which: 36 });
    expect(slider).toHaveAttribute('aria-valuenow', '0');
    expect(
      within(graph).getByRole('button', {
        name: /主审核 Agent.*模型调用 0 次/,
      }),
    ).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole('combobox', { name: '回放倍速' }));
    fireEvent.click(
      screen.getByText('4×', { selector: '.ant-select-item-option-content' }),
    );
    fireEvent.click(screen.getByRole('button', { name: '回到实时' }));
    expect(
      screen.queryByRole('slider', { name: '回放进度' }),
    ).not.toBeInTheDocument();
    expect(
      within(graph).getByRole('button', {
        name: /主审核 Agent.*模型调用 1 次/,
      }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '回放执行' }));
    fireEvent.click(screen.getByRole('radio', { name: '轨迹树' }));
    fireEvent.click(screen.getByRole('radio', { name: 'Agent 图' }));
    expect(
      screen.queryByRole('slider', { name: '回放进度' }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '回放执行' })).toBeEnabled();
    expect(fetch.mock.calls).toHaveLength(requests);
  });
  it('工具组只列实际调用，点击成员才读取详情；可回到所属图节点', async () => {
    const main = { ...span('main'), kind: 'batch', title: '主审核会话' };
    const call = {
      ...span('call'),
      kind: 'tool',
      parent_span_id: 'main',
      tool_name: 'search_documents',
      name: 'search_documents',
      title: 'search_documents',
    };
    const fetch = mockApi((url) =>
      url.includes('/progress')
        ? progressData([main, call])
        : url.includes('/trace/call')
          ? {
              span: call,
              input: { query_list: [] },
              output: '原文检索完成',
              error: null,
              truncated: false,
            }
          : undefined,
    );
    render(
      <App>
        <Progress />
      </App>,
    );
    await screen.findByText('当前文件 · 已观测');
    fireEvent.click(screen.getByRole('radio', { name: 'Agent 图' }));
    const graph = await screen.findByRole('region', {
      name: 'Agent 执行图',
    });
    fireEvent.click(
      within(graph).getByRole('button', { name: /原文检索.*调用 1 次/ }),
    );
    expect(fetch.mock.calls.some(([url]) => url.includes('/trace/'))).toBe(
      false,
    );
    fireEvent.click(await screen.findByRole('button', { name: '原文检索' }));
    expect(await screen.findByText('原文检索完成')).toBeInTheDocument();
    expect(
      fetch.mock.calls.filter(([url]) => url.includes('/trace/call')),
    ).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: '在图中定位' }));
    expect(screen.getByRole('radio', { name: 'Agent 图' })).toBeChecked();
    expect(
      fetch.mock.calls.filter(([url]) => url.includes('/progress')),
    ).toHaveLength(1);
  });
  it('完成任务仍排空增量页，忽略跨文件span，不把状态更新计为新调用', async () => {
    const fetch = mockApi((url) => {
      if (!url.includes('/progress')) return undefined;
      return url.includes('after_seq=150')
        ? {
            ...progressData(),
            trace_updates: [
              { seq: 151, span: span('same') },
              { seq: 152, span: { ...span('foreign'), file_id: 'file-other' } },
            ],
            next_seq: 160,
          }
        : {
            ...progressData(),
            trace_updates: [{ seq: 1, span: span('same', 'running') }],
            next_seq: 150,
            has_more: true,
          };
    });
    render(
      <App>
        <Progress />
      </App>,
    );
    await waitFor(() =>
      expect(
        fetch.mock.calls.filter(([url]) => url.includes('/progress')),
      ).toHaveLength(2),
    );
    await waitFor(() =>
      expect(
        screen.queryByText('正在加载剩余轨迹；当前为部分记录'),
      ).not.toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole('button', { name: '执行记录' }));
    expect(await screen.findByText(/已加载 1 个独立节点/)).toBeInTheDocument();
    expect(
      fetch.mock.calls.some(([url]) => url.includes('after_seq=150')),
    ).toBe(true);
  });
  it('调用详情归属不符时禁止展示内容', async () => {
    const fetch = mockApi((url) =>
      url.includes('/trace/')
        ? {
            span: { ...span('span-1'), file_id: 'other' },
            input: null,
            output: '别的文件机密',
            error: null,
            truncated: false,
          }
        : undefined,
    );
    render(
      <App>
        <Progress />
      </App>,
    );
    await screen.findByText('当前文件 · 已观测');
    fireEvent.click(screen.getByRole('button', { name: '执行记录' }));
    fireEvent.click(await screen.findByRole('button', { name: '模型调用' }));
    expect(
      await screen.findByText('调用详情归属不符，已阻止展示'),
    ).toBeInTheDocument();
    expect(screen.queryByText('别的文件机密')).not.toBeInTheDocument();
    expect(
      fetch.mock.calls.some(([url]) => url.includes('/trace/span-1')),
    ).toBe(true);
  });
  it('切换文件取消旧进度，迟到响应不能覆盖当前图与计数', async () => {
    const second = {
      ...file,
      file_id: 'file-2',
      file_name: '标书二.md',
      source_path: '乙/标书二.md',
    };
    let finish: ((value: Response) => void) | undefined;
    let oldSignal: AbortSignal | null | undefined;
    const delayed = new Promise<Response>((resolve) => {
      finish = resolve;
    });
    const response = (body: unknown) =>
      new Response(JSON.stringify(body), {
        headers: { 'Content-Type': 'application/json' },
      });
    const fetch = vi.fn((input: string, init?: RequestInit) => {
      if (input.includes('/progress') && input.includes('file_id=file-1')) {
        oldSignal = init?.signal;
        return delayed;
      }
      if (input.includes('/progress'))
        return Promise.resolve(
          response({
            ...progressData([{ ...span('second'), file_id: 'file-2' }], second),
            files: [file, second],
            activity: {
              ...progressData().activity,
              file_id: 'file-2',
              model_calls: 7,
            },
          }),
        );
      return Promise.resolve(response({ task, files: [file, second] }));
    });
    vi.stubGlobal('fetch', fetch);
    render(
      <App>
        <Progress />
      </App>,
    );
    await screen.findByRole('combobox', { name: '当前投标文件' });
    await waitFor(() => expect(oldSignal).toBeDefined());
    fireEvent.mouseDown(screen.getByRole('combobox', { name: '当前投标文件' }));
    fireEvent.click(
      await screen.findByText('标书二.md', {
        selector: '.ant-select-item-option-content',
      }),
    );
    await screen.findByText('当前文件 · 已观测');
    expect(document.querySelector('.audit-observed')?.textContent).toContain(
      '模型调用7',
    );
    expect(oldSignal?.aborted).toBe(true);
    await act(async () => {
      finish?.(
        response({
          ...progressData(),
          activity: { ...progressData().activity, model_calls: 99 },
        }),
      );
      await delayed;
    });
    expect(document.querySelector('.audit-observed')?.textContent).toContain(
      '模型调用7',
    );
    expect(
      document.querySelector('.audit-observed')?.textContent,
    ).not.toContain('99');
    fireEvent.click(screen.getByRole('button', { name: '执行记录' }));
    expect(await screen.findByText(/已加载 1 个独立节点/)).toBeInTheDocument();
  });
  it('正式报告读取失败仍能换文件和查看处理过程', async () => {
    const response = (body: unknown, status = 200) =>
      new Response(JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
      });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string) =>
        input.endsWith('/files/file-1')
          ? response(
              {
                error: {
                  code: 'report_unavailable',
                  message: '报告正文不可读取',
                },
              },
              500,
            )
          : response({ task, files: [file] }),
      ),
    );
    render(
      <App>
        <Report />
      </App>,
    );
    expect(await screen.findByText('报告正文不可读取')).toBeInTheDocument();
    expect(
      screen.getByRole('combobox', { name: '切换报告文件' }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: '查看处理过程' }),
    ).toBeInTheDocument();
    expect(screen.queryByText('项目不匹配')).not.toBeInTheDocument();
  });
});
