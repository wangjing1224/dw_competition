// @vitest-environment jsdom
import { render, screen, cleanup, waitFor } from '@testing-library/react';
import { App } from 'antd';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import Workbench from '../Workbench';
import Libraries from './List';
import LibraryDetail from './Detail';
import { validateFiles } from './Create';
import { safeMarkdown } from './Detail/SourcePreview';
import { mergeChanges } from './Progress';
import {
  api,
  ApiError,
  download,
  type TraceNode,
} from '@/services/ruleLibraries';

vi.mock('@umijs/max', () => ({
  Link: ({ to, children, ...props }: { to: string; children: ReactNode }) => (
    <a href={to} {...props}>
      {children}
    </a>
  ),
  history: { push: vi.fn() },
  useParams: () => ({ projectId: 'project-1' }),
}));
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('规则库工作台', () => {
  it('首页只有业务入口，不请求统计、不展示使用说明条', () => {
    const fetch = vi.fn();
    vi.stubGlobal('fetch', fetch);
    render(
      <App>
        <Workbench />
      </App>,
    );
    expect(
      screen.getByRole('heading', { name: '招投标智能审核工作台' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '规则库' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '创建审核任务' })).toBeDisabled();
    expect(screen.queryByText('结果如何使用')).not.toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });
  it('列表空状态不回退到假数据', async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        items: [],
        total: 0,
        overview: {
          project_count: 0,
          ready_count: 0,
          processing_count: 0,
          failed_count: 0,
          attention_count: 0,
        },
      }),
    });
    vi.stubGlobal('fetch', fetch);
    render(
      <App>
        <Libraries />
      </App>,
    );
    expect(await screen.findByText('暂无符合条件的规则库')).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it('列表展示后端错误而非伪装空列表', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        json: async () => ({
          error: { code: 'database_failed', message: '数据库读取失败' },
        }),
      }),
    );
    render(
      <App>
        <Libraries />
      </App>,
    );
    expect(await screen.findByText('数据库读取失败')).toBeInTheDocument();
    expect(screen.queryByText('暂无符合条件的规则库')).not.toBeInTheDocument();
  });
  it('文件验证保持目录信息并拒绝重复、空文件和不支持的格式', () => {
    const a = new File(['正文'], '招标.md');
    const b = new File(['正文'], '招标.md');
    Object.defineProperty(b, 'webkitRelativePath', { value: '附件/招标.md' });
    const result = validateFiles(
      [a, b, a, new File([], '空.pdf'), new File(['x'], '压缩包.zip')],
      [],
    );
    expect(result.accepted.map((f) => f.path)).toEqual([
      '招标.md',
      '附件/招标.md',
    ]);
    expect(result.errors).toHaveLength(3);
  });
  it('正式空库仅索引异常时仍允许人工新增和导出', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          project: {
            project_id: 'project-1',
            display_name: '正式空库',
            status: 'rule_ready',
          },
          revision: 'sha256:test',
          snapshot_kind: 'final',
          warning: {
            code: 'tender_index_unavailable',
            message: '索引不可用',
          },
          available_for_audit: false,
          rule_set: { project_profile: '', rules: [], source_files: [] },
          files: [],
        }),
      }),
    );
    render(
      <App>
        <LibraryDetail />
      </App>,
    );
    await screen.findByRole('heading', { name: '正式空库' });
    expect(screen.getByRole('button', { name: /新增规则/ })).toBeEnabled();
    expect(screen.getByRole('button', { name: /JSON/ })).toBeEnabled();
    expect(screen.getByText('索引不可用')).toBeInTheDocument();
  });
  it('坏快照保留详情但禁用维护和导出', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          project: {
            project_id: 'project-1',
            display_name: '快照异常',
            status: 'rule_ready',
          },
          revision: null,
          snapshot_kind: 'final',
          warning: { code: 'rule_set_unavailable', message: '规则快照损坏' },
          available_for_audit: false,
          rule_set: null,
          files: [],
        }),
      }),
    );
    render(
      <App>
        <LibraryDetail />
      </App>,
    );
    await screen.findByRole('heading', { name: '快照异常' });
    expect(screen.getByRole('button', { name: /新增规则/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: /JSON/ })).toBeDisabled();
    expect(screen.getByRole('tab', { name: '来源文件' })).toBeInTheDocument();
  });
  it('Markdown 保留表格但只加载已登记的同源图片', () => {
    const html = safeMarkdown(
      '<script>alert(1)</script><table><tr><td onclick="bad()">正文</td></tr></table>\n\n![图片](file-1/images/1.png)\n\n<img src="https://remote.invalid/x" onerror="bad()"><iframe src="https://remote.invalid"></iframe>',
      [
        {
          path: 'file-1/images/1.png',
          url: '/api/projects/project-1/files/file-1/images/1.png?version=abc',
          description: '',
          purpose: 'content',
          read_status: 'readable',
        },
      ],
      '正文',
    );
    expect(html).toContain('<table>');
    expect(html).toContain('<mark>正文</mark>');
    expect(html).toContain('/api/projects/project-1/files/');
    expect(html).not.toMatch(
      /<script|onclick|onerror|<iframe|https:\/\/remote/,
    );
  });
  it('节点按 span_id 和 seq 合并，旧更新不会覆盖新状态', () => {
    const base = { span_id: 's1', status: 'running' } as TraceNode;
    const merged = mergeChanges({}, [
      { seq: 1, node: base },
      { seq: 3, node: { ...base, status: 'succeeded' } },
      { seq: 2, node: base },
    ]);
    expect(merged.s1.seq).toBe(3);
    expect(merged.s1.node.status).toBe('succeeded');
  });
  it('API 保留409冲突状态供页面处理', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 409,
        json: async () => ({
          error: { code: 'rule_set_revision_conflict', message: '版本变化' },
        }),
      }),
    );
    await expect(api('/project-1/edit')).rejects.toEqual(
      new ApiError(409, 'rule_set_revision_conflict', '版本变化'),
    );
  });
  it('下载失败的JSON不会保存成文件', async () => {
    const create = vi.spyOn(URL, 'createObjectURL');
    create.mockClear();
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 409,
        json: async () => ({ error: { message: '尚未完成' } }),
      }),
    );
    await expect(
      download('/project-1/export?format=json', 'rules.json'),
    ).rejects.toThrow('尚未完成');
    await waitFor(() => expect(create).not.toHaveBeenCalled());
  });
});
