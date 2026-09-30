// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { App } from 'antd';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { layout } from '@/app';
import {
  ApiError,
  api,
  download,
  type TraceNode,
} from '@/services/ruleLibraries';
import Workbench from '../Workbench';
import { validateFiles } from './Create';
import LibraryDetail from './Detail';
import { safeMarkdown } from './Detail/SourcePreview';
import Libraries from './List';
import ProgressPage, { mergeChanges } from './Progress';
import { PageHeading } from './shared';

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
  it('规则按完整库编号，搜索/编辑只换展示，不改提交的真实 ID', async () => {
    const source = {
      file_id: 'file-38',
      file_name: '招标文件.md',
      source_path: '采购/招标文件.md',
    };
    const rules = ['rule-147', 'rule-193'].map((rule_id, i) => ({
      rule_id,
      category: 'technical',
      priority: 'important',
      verification: 'bid_only',
      provenance: 'ai_generated',
      requirement: i ? '设备精度至少100' : '设备数量至少10',
      reason: '依据技术要求',
      sources: [source],
    }));
    const detail = {
      project: {
        project_id: 'project-1',
        display_name: '设备规则库',
        status: 'rule_ready',
      },
      revision: 'rev1',
      rule_set: { rules, project_profile: '设备采购', source_files: [source] },
      files: [source],
      warning: null,
      available_for_audit: true,
    };
    const fetch = vi.fn(async (_url: string, init?: RequestInit) => ({
      ok: true,
      json: async () =>
        init?.method === 'POST' ? { revision: 'rev2' } : detail,
    }));
    vi.stubGlobal('fetch', fetch);
    render(
      <App>
        <LibraryDetail />
      </App>,
    );
    await screen.findByText('规则 02');
    expect(screen.queryByText('rule-193')).not.toBeInTheDocument();
    expect(screen.queryByText('project-1')).not.toBeInTheDocument();
    const search = screen.getByPlaceholderText('搜索规则编号、内容或依据说明');
    fireEvent.change(search, { target: { value: '规则02' } });
    expect(screen.queryByText('规则 01')).not.toBeInTheDocument();
    expect(screen.getByText('规则 02')).toBeInTheDocument();
    // 技术排查仍能用真实 ID 搜索；得到的仍是规则 02，而非筛选后的规则 01。
    fireEvent.change(search, { target: { value: 'rule-193' } });
    fireEvent.click(screen.getByRole('button', { name: /编辑$/ }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('编辑规则 02')).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'OK' }));
    fireEvent.click(await screen.findByRole('button', { name: '确认保存' }));
    await waitFor(() =>
      expect(fetch.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(
        true,
      ),
    );
    const saved = fetch.mock.calls.find(([, init]) => init?.method === 'POST');
    if (!saved) throw new Error('未提交规则保存请求');
    const [url, init] = saved;
    expect(url).toBe('/api/projects/project-1/edit');
    expect(JSON.parse(String(init?.body)).data).toMatchObject({
      rule_id: 'rule-193',
      source_file_ids: ['file-38'],
    });
  });
  it('页面标题提供明确的返回入口，子页面默认返回规则库', () => {
    const { rerender } = render(<PageHeading title="新建规则库" />);
    expect(screen.getByRole('link', { name: '返回规则库' })).toHaveAttribute(
      'href',
      '/rule-libraries',
    );
    rerender(<PageHeading title="处理进度" />);
    expect(screen.getAllByRole('link', { name: '返回规则库' })).toHaveLength(1);
    rerender(<PageHeading title="规则详情" />);
    expect(screen.getByRole('link', { name: '返回规则库' })).toHaveAttribute(
      'href',
      '/rule-libraries',
    );
  });
  it('侧栏收起时隐藏两段说明，展开后恢复', () => {
    const config = layout({} as Parameters<typeof layout>[0]);
    const actions = config.actionsRender;
    const footer = config.menuFooterRender;
    if (typeof actions !== 'function' || typeof footer !== 'function') {
      throw new Error('布局应提供侧栏渲染函数');
    }
    const sidebar = (collapsed: boolean) => (
      <>
        {config.logo}
        {actions({ collapsed } as Parameters<typeof actions>[0])}
        {footer({ collapsed } as Parameters<typeof footer>[0])}
      </>
    );
    const { rerender } = render(sidebar(false));
    expect(
      screen.getByText('招投标智能审核 · 规则 · 资料 · 审核'),
    ).toBeInTheDocument();
    expect(screen.getByText(/智能整理 · 依据可查/)).toBeInTheDocument();
    rerender(sidebar(true));
    expect(
      screen.queryByText('招投标智能审核 · 规则 · 资料 · 审核'),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/智能整理 · 依据可查/)).not.toBeInTheDocument();
    expect(screen.getByText('审')).toBeInTheDocument();
    rerender(sidebar(false));
    expect(screen.getByText(/智能整理 · 依据可查/)).toBeInTheDocument();
  });
  it('切换有数据和空状态时保持相同的满宽容器，空提示在列表内部', async () => {
    const styles = readFileSync('src/global.less', 'utf8');
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(async (input: string) => {
        const status = new URL(input, 'http://localhost').searchParams.get(
          'status',
        );
        const items =
          !status || status === 'rule_ready'
            ? [
                {
                  project_id: 'project-1',
                  display_name: '布局测试库',
                  status: 'rule_ready',
                  updated_at: '2026-09-27T10:00:00Z',
                  file_count: 1,
                  rule_count: 71,
                  category_counts: { technical: 71 },
                  warning: null,
                },
              ]
            : [];
        return {
          ok: true,
          json: async () => ({
            items,
            total: items.length,
            overview: {
              project_count: 1,
              ready_count: 1,
              processing_count: 0,
              failed_count: 0,
              attention_count: 0,
            },
          }),
        };
      }),
    );
    render(
      <>
        <style>{styles}</style>
        <App>
          <Libraries />
        </App>
      </>,
    );
    await screen.findByRole('heading', { name: '布局测试库' });
    expect(document.querySelectorAll('.library-metric')).toHaveLength(2);
    expect(screen.getByRole('link', { name: '返回工作台' })).toHaveAttribute(
      'href',
      '/home',
    );
    const page = screen.getByRole('main');
    const heading = screen.getByRole('heading', { name: '规则库', level: 1 });
    const pagination = screen.getByRole('navigation', { name: '规则库分页' });
    const controls = pagination.querySelector(
      '.list-pagination-controls',
    ) as HTMLElement;
    const pageItems = controls.querySelectorAll('.ant-pagination > li').length;
    for (const name of ['已完成', '处理中', '失败', '全部']) {
      fireEvent.click(screen.getByRole('radio', { name }));
      if (name === '处理中' || name === '失败') {
        const empty = await screen.findByText(
          name === '处理中' ? '暂无正在处理的规则库' : '暂无处理失败的规则库',
        );
        expect(empty.closest('.library-grid')).not.toBeNull();
        expect(
          within(empty.closest('.library-empty') as HTMLElement).queryByRole(
            'button',
          ),
        ).not.toBeInTheDocument();
        expect(
          within(pagination).getByText('共 0 个规则库'),
        ).toBeInTheDocument();
        expect(controls).not.toBeVisible();
        expect(controls).toHaveAttribute('aria-hidden', 'true');
        expect(controls).toHaveAttribute('inert');
        expect(within(controls).queryAllByRole('button')).toHaveLength(0);
        expect(
          screen.queryByRole('heading', { name: '布局测试库' }),
        ).not.toBeInTheDocument();
      } else {
        await screen.findByRole('heading', { name: '布局测试库' });
        expect(controls).toBeVisible();
        expect(controls).not.toHaveAttribute('inert');
      }
      // 0 条和 1 条使用同一套页码占位，仅改变可见性，不移除布局元素。
      expect(pagination.querySelector('.list-pagination-controls')).toBe(
        controls,
      );
      expect(controls.querySelector('.ant-pagination')).toBeInTheDocument();
      expect(controls.querySelectorAll('.ant-pagination > li')).toHaveLength(
        pageItems,
      );
      expect(getComputedStyle(controls).display).not.toBe('none');
      expect(getComputedStyle(controls).flexShrink).toBe('0');
      expect(screen.getByRole('main')).toBe(page);
      expect(screen.getByRole('heading', { name: '规则库', level: 1 })).toBe(
        heading,
      );
      // jsdom不计算像素布局；这里锁定修复所依赖的宽度契约，而非冒充浏览器视觉测试。
      expect(getComputedStyle(page).width).toBe('100%');
      expect(getComputedStyle(page).minWidth).toBe('0px');
      expect(getComputedStyle(page).marginTop).toBe('0px');
      expect(getComputedStyle(page).display).toBe('flex');
      expect(getComputedStyle(page).flexDirection).toBe('column');
      expect(
        getComputedStyle(page.querySelector('.library-results') as HTMLElement)
          .flexGrow,
      ).toBe('1');
      expect(getComputedStyle(pagination).flexShrink).toBe('0');
      expect(screen.getByRole('navigation', { name: '规则库分页' })).toBe(
        pagination,
      );
    }
  });
  it.each([
    0, 24,
  ])('进度页的三个面板统一约束高度并独立滚动（%i份文件）', async (fileCount) => {
    const files = Array.from({ length: fileCount }, (_, i) => ({
      file_id: `file-${i + 1}`,
      file_name: `来源文件${i + 1}.docx`,
      source_path: `材料/来源文件${i + 1}.docx`,
      parse_status: 'parsed',
      extraction_status: 'completed',
    }));
    const rules = fileCount
      ? Array.from({ length: 3 }, (_, i) => ({
          rule_id: `rule-${147 + i}`,
          category: 'technical',
          priority: 'critical',
          verification: 'bid_only',
          provenance: 'ai_generated',
          requirement: '用于验证长规则仅在右栏滚动，不撑高整页。'.repeat(25),
          reason: '依据当前文件明确要求。',
          sources: [],
        }))
      : [];
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(async (input: string) => ({
        ok: true,
        json: async () =>
          input.includes('/progress')
            ? {
                project_id: 'project-1',
                display_name: '进度布局测试',
                status: 'rule_ready',
                phase: 'completed',
                document_count: fileCount,
                completed_file_ids: files.map((file) => file.file_id),
                saved_rule_count: rules.length,
                files,
                recent_rules: rules,
                rule_order: [
                  'rule-11',
                  'rule-12',
                  ...rules.map((r) => r.rule_id),
                ],
                error: null,
                trace: {
                  trace_status: 'completed',
                  changes: files.map((file, i) => ({
                    seq: i + 1,
                    node: {
                      span_id: `span-${i + 1}`,
                      parent_span_id: null,
                      kind: 'file',
                      title: `执行记录${i + 1}`,
                      status: 'succeeded',
                      started_at: '2026-09-28T10:00:00Z',
                      duration_ms: 1000,
                      has_detail: false,
                      file_id: file.file_id,
                    },
                  })),
                  next_cursor: fileCount,
                  has_more: false,
                  warning: null,
                },
              }
            : {
                project: { project_id: 'project-1', status: 'rule_ready' },
                rule_set: { rules },
                warning: null,
              },
      })),
    );
    render(
      <>
        <style>{readFileSync('src/global.less', 'utf8')}</style>
        <App>
          <ProgressPage />
        </App>
      </>,
    );
    await screen.findByText(
      fileCount ? '处理结果已保存' : '处理完成，但未提取到规则',
    );
    const page = screen.getByRole('main');
    expect(page).toHaveClass('business-page', 'rule-progress-page');
    expect(getComputedStyle(page).width).toBe('100%');
    expect(getComputedStyle(page).marginTop).toBe('0px');
    expect(getComputedStyle(page).display).toBe('flex');
    const grid = page.querySelector('.trace-grid') as HTMLElement;
    expect(getComputedStyle(grid).gridTemplateRows).toBe('minmax(0, 1fr)');
    expect(getComputedStyle(grid).alignItems).toBe('stretch');
    expect(getComputedStyle(grid).flexGrow).toBe('1');
    for (const label of [
      '来源文件列表',
      'Agent 执行轨迹内容',
      '最近保存的规则内容',
    ]) {
      const body = screen.getByRole('region', { name: label });
      const card = body.closest('.trace-panel') as HTMLElement;
      const header = card.querySelector('.ant-card-head') as HTMLElement;
      // jsdom不计算实际几何尺寸；锁定等高、滚动区域与标题分离的样式契约。
      expect(getComputedStyle(card).display).toBe('flex');
      expect(getComputedStyle(card).minHeight).toBe('0px');
      expect(getComputedStyle(header).flexShrink).toBe('0');
      expect(body).toHaveAttribute('tabindex', '0');
      expect(getComputedStyle(body).overflow).toBe('auto');
      expect(body.contains(header)).toBe(false);
      expect(body.querySelector('.trace-panel-scroll')).toBeNull();
    }
    expect(page.querySelectorAll('.file-progress')).toHaveLength(fileCount);
    expect(page.querySelectorAll('.recent-rules .rule-card')).toHaveLength(
      rules.length,
    );
    if (fileCount) {
      expect(
        within(page.querySelector('.recent-rules') as HTMLElement).getByText(
          '规则 03',
        ),
      ).toBeInTheDocument();
      expect(screen.queryByText('rule-147')).not.toBeInTheDocument();
    }
  });
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
    expect(screen.getByRole('button', { name: '创建审核任务' })).toBeEnabled();
    expect(screen.getByRole('link', { name: '创建审核任务' })).toHaveAttribute(
      'href',
      '/audit-tasks/new',
    );
    expect(screen.queryByText('结果如何使用')).not.toBeInTheDocument();
    expect(
      screen.queryByRole('link', { name: /返回/ }),
    ).not.toBeInTheDocument();
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
    expect(await screen.findByText('尚未创建规则库')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: /新建规则库/ })).toHaveLength(
      2,
    );
    expect(fetch).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('radio', { name: '失败' }));
    await screen.findByText('暂无处理失败的规则库');
    expect(screen.getAllByRole('button', { name: /新建规则库/ })).toHaveLength(
      1,
    );
    fireEvent.change(screen.getByPlaceholderText('搜索规则库名称或 ID'), {
      target: { value: '不存在的名称' },
    });
    await screen.findByText('暂无符合条件的规则库');
    expect(screen.getAllByRole('button', { name: /新建规则库/ })).toHaveLength(
      1,
    );
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
          url: `/api/projects/project-1/files/file-1/preview?image_name=1.png&version=${'a'.repeat(64)}`,
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
