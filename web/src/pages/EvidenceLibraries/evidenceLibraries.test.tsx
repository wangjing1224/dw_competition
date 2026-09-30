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
import { safeMarkdown, validImageUrl } from '@/components/SourcePreview';
import type { Detail, Material } from '@/services/evidenceLibraries';
import Create from './Create';
import LibraryDetail, { directRelations } from './Detail';
import Libraries from './List';
import Progress from './Progress';

vi.mock('@umijs/max', () => ({
  Link: ({ to, children, ...props }: { to: string; children: ReactNode }) => (
    <a href={to} {...props}>
      {children}
    </a>
  ),
  history: { push: vi.fn() },
  useParams: () => ({ bidderId: 'bidder-1' }),
}));
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
});
const source = {
  file_id: 'file-1',
  file_name: '合同.md',
  source_path: '业绩/合同.md',
  extension: '.md',
  size_bytes: 1024,
  parse_status: 'parsed',
  ingestion_status: 'completed',
  has_material: true,
  has_reading_limit: false,
  provenance: 'ai_generated' as const,
};
const material: Material = {
  file_id: 'file-1',
  provenance: 'ai_generated',
  bid_summary: '设备供货合同',
  evidence_summary: '## 合同范围\n\n约定12台设备；不证明交付。',
  relations: [],
};
const detail: Detail = {
  bidder: {
    bidder_id: 'bidder-1',
    display_name: '华衡资料库',
    status: 'evidence_ready',
    created_at: '2026-09-28T00:00:00Z',
    updated_at: '2026-09-28T00:01:00Z',
  },
  library: {
    bidder_id: 'bidder-1',
    source_files: [source],
    materials: [material],
  },
  files: [source],
  revision: 'v1',
  snapshot_kind: 'final',
  editable: true,
  exportable: true,
  available_for_audit: true,
  warning: null,
  error: null,
};
function reply(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body };
}

describe('投标方资料库', () => {
  it('资料目录按直接关系双向显示，不推断传递关系', () => {
    const records: Material[] = [
      material,
      { ...material, file_id: 'file-2', relations: [['对应验收', 'file-1']] },
      { ...material, file_id: 'file-3', relations: [['补充说明', 'file-2']] },
    ];
    expect(directRelations(records, 'file-1')).toEqual([
      { from: 'file-2', to: 'file-1', relation: '对应验收' },
    ]);
  });

  it('列表无筛选结果不显示新建按钮，分页保留同一占位', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(async (url: string) =>
        reply({
          items: url.includes('status=evidence_failed')
            ? []
            : [
                {
                  ...detail.bidder,
                  file_count: 1,
                  material_count: 1,
                  has_reading_limit: true,
                  available_for_audit: true,
                  warning: null,
                },
              ],
          total: url.includes('status=evidence_failed') ? 0 : 1,
          page: 1,
          page_size: 12,
          overview: {
            library_count: 1,
            ready_count: 1,
            processing_count: 0,
            attention_count: 0,
          },
        }),
      ),
    );
    render(
      <App>
        <Libraries />
      </App>,
    );
    await screen.findByRole('heading', { name: '华衡资料库' });
    expect(screen.queryByText('含读取限制')).not.toBeInTheDocument();
    expect(document.querySelectorAll('.library-metric')).toHaveLength(2);
    expect(
      screen.getByText('可供审核选择', { selector: '.ant-tag' }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByText('失败', { exact: true }));
    await screen.findByText('暂无处理失败的资料库');
    expect(screen.getAllByRole('button', { name: /新建资料库/ })).toHaveLength(
      1,
    );
    const pager = screen.getByRole('navigation', { name: '资料库分页' });
    expect(pager.querySelector('[aria-hidden="true"]')).toHaveAttribute(
      'inert',
    );
    expect(within(pager).getByText('共 0 个资料库')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '返回工作台' })).toHaveAttribute(
      'href',
      '/home',
    );
  });

  it('新建页单栏、复用文件校验、列表内滚动且重复路径不重复加入', async () => {
    render(
      <App>
        <Create />
      </App>,
    );
    expect(document.querySelector('.evidence-create aside')).toBeNull();
    expect(screen.getByRole('link', { name: '返回资料库' })).toHaveAttribute(
      'href',
      '/evidence-libraries',
    );
    const input = document.querySelector(
      'input[type=file]',
    ) as HTMLInputElement;
    const file = new File(['合同'], '合同.md', { type: 'text/markdown' });
    fireEvent.change(input, { target: { files: [file, file] } });
    expect(await screen.findByText('合同.md：重复路径')).toBeInTheDocument();
    expect(screen.getByText('已选择 1 份文件')).toBeInTheDocument();
    expect(document.querySelector('.ant-table-body')).toHaveStyle({
      overflowY: 'scroll',
    });
  });

  it('刷新后已有提交编号只查询，不自动重传', async () => {
    sessionStorage.setItem('evidence-library-pending-submission', 'request-id');
    const fetch = vi.fn().mockResolvedValue(reply({ items: [], total: 0 }));
    vi.stubGlobal('fetch', fetch);
    render(
      <App>
        <Create />
      </App>,
    );
    await screen.findByText('正在确认提交结果');
    expect(fetch).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '查询提交结果' }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    expect(fetch.mock.calls[0][0]).toContain(
      '/api/bidders?client_request_id=request-id',
    );
    expect(fetch.mock.calls[0][1].method).toBeUndefined();
  });

  it('正式详情显示摘要、来源标记和正确业务返回地址', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(reply(detail)));
    render(
      <App>
        <LibraryDetail />
      </App>,
    );
    await screen.findByRole('heading', { name: '华衡资料库' });
    expect(
      screen.getByRole('heading', { name: '合同范围' }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: '编辑摘要与关系' }),
    ).toBeEnabled();
    expect(screen.getByRole('link', { name: '返回资料库' })).toHaveAttribute(
      'href',
      '/evidence-libraries',
    );
    expect(screen.getAllByText('AI 整理').length).toBeGreaterThan(0);
    expect(screen.queryByText('file-1')).not.toBeInTheDocument();
    expect(screen.queryByText('bidder-1')).not.toBeInTheDocument();
    expect(
      document.querySelector('[data-entity-id="file-1"]'),
    ).toHaveTextContent('合同.md');
  });

  it('资料目录只按全部、AI 整理、人工修正筛选，不显示读取限制标签', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        reply({
          ...detail,
          files: [
            { ...source, has_reading_limit: true },
            {
              ...source,
              file_id: 'file-2',
              file_name: '验收.md',
              source_path: '业绩/验收.md',
              provenance: 'human_modified',
            },
            {
              ...source,
              file_id: 'file-3',
              file_name: '待整理.md',
              source_path: '业绩/待整理.md',
              provenance: null,
            },
          ],
          library: {
            ...detail.library,
            materials: [
              material,
              {
                ...material,
                file_id: 'file-2',
                provenance: 'human_modified',
                relations: [['支持', 'file-1']],
              },
            ],
          },
        }),
      ),
    );
    render(
      <App>
        <LibraryDetail />
      </App>,
    );
    const directory = await screen.findByRole('navigation', {
      name: '资料文件目录',
    });
    await within(directory).findByRole('button', { name: /合同.md/ });
    expect(within(directory).getAllByRole('button')).toHaveLength(3);
    expect(
      screen.queryByText('读取限制', { exact: true }),
    ).not.toBeInTheDocument();
    const choose = (label: string) => {
      fireEvent.mouseDown(
        screen.getByRole('combobox', { name: '资料来源筛选' }),
      );
      expect(screen.queryByText('有读取限制')).not.toBeInTheDocument();
      expect(screen.queryByText('有直接关联')).not.toBeInTheDocument();
      fireEvent.click(
        screen.getByText(label, {
          selector: '.ant-select-item-option-content',
        }),
      );
    };
    choose('AI 整理');
    expect(within(directory).getAllByRole('button')).toHaveLength(1);
    expect(
      within(directory).getByRole('button', { name: /合同.md/ }),
    ).toBeInTheDocument();
    choose('人工修正');
    expect(within(directory).getAllByRole('button')).toHaveLength(1);
    expect(
      within(directory).getByRole('button', { name: /验收.md/ }),
    ).toBeInTheDocument();
    choose('全部');
    expect(within(directory).getAllByRole('button')).toHaveLength(3);
  });

  it('同名创建失败保留名称和文件，允许修改后重新提交', async () => {
    const fetch = vi.fn().mockResolvedValue(
      reply(
        {
          error: {
            code: 'duplicate_library_name',
            message: '资料库名称已存在，请更换名称',
          },
        },
        409,
      ),
    );
    vi.stubGlobal('fetch', fetch);
    render(
      <App>
        <Create />
      </App>,
    );
    const name = screen.getByLabelText(/资料库名称/);
    fireEvent.change(name, { target: { value: '重复名称' } });
    fireEvent.change(
      document.querySelector('input[type=file]') as HTMLInputElement,
      {
        target: {
          files: [new File(['合同'], '合同.md', { type: 'text/markdown' })],
        },
      },
    );
    fireEvent.click(screen.getByRole('button', { name: /开始整理/ }));
    await screen.findByText('资料库名称已存在，请更换名称');
    expect(name).toHaveValue('重复名称');
    expect(name).toBeEnabled();
    expect(screen.getByText('已选择 1 份文件')).toBeInTheDocument();
    expect(
      sessionStorage.getItem('evidence-library-pending-submission'),
    ).toBeNull();
  });

  it('损坏快照仍可查看原件，但不能编辑、导出或伪造空资料', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        reply({
          ...detail,
          library: null,
          revision: null,
          editable: false,
          exportable: false,
          warning: {
            code: 'evidence_library_unavailable',
            message: '资料损坏',
          },
        }),
      ),
    );
    render(
      <App>
        <LibraryDetail />
      </App>,
    );
    await screen.findByText('资料损坏');
    expect(screen.getByRole('button', { name: /导出 JSON/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: '查看解析原文' })).toBeEnabled();
    expect(screen.getByText('资料快照不可读取')).toBeInTheDocument();
  });

  it('保存只发单文件内容和 If-Match，409 保留草稿', async () => {
    const fetch = vi
      .fn()
      .mockImplementation(async (_url: string, init?: RequestInit) =>
        init?.method === 'POST'
          ? reply(
              {
                error: {
                  code: 'evidence_revision_conflict',
                  message: '资料库已变化，草稿未保存',
                },
              },
              409,
            )
          : reply(detail),
      );
    vi.stubGlobal('fetch', fetch);
    render(
      <App>
        <LibraryDetail />
      </App>,
    );
    fireEvent.click(
      await screen.findByRole('button', { name: '编辑摘要与关系' }),
    );
    fireEvent.change(screen.getByLabelText('简洁摘要'), {
      target: { value: '我的修改草稿' },
    });
    fireEvent.click(screen.getByRole('button', { name: '对比并保存' }));
    fireEvent.click(await screen.findByRole('button', { name: '确认保存' }));
    await screen.findByText('资料库已变化，草稿未保存');
    expect(screen.getByLabelText('简洁摘要')).toHaveValue('我的修改草稿');
    const call = fetch.mock.calls.find(([, init]) => init?.method === 'POST');
    expect(call?.[1]?.headers).toEqual({
      'Content-Type': 'application/json',
      'If-Match': '"v1"',
    });
    expect(JSON.parse(call?.[1]?.body as string).data).toEqual({
      ...material,
      provenance: undefined,
      bid_summary: '我的修改草稿',
    });
  });

  it('原文图片仅加载本次登记的业务文件和版本', () => {
    const version = 'a'.repeat(64),
      baseUrl = '/api/bidders/bidder-1/files/file-1/preview';
    const image = {
      path: 'file-1/images/1.png',
      url: `${baseUrl}?image_name=1.png&version=${version}`,
      purpose: 'content',
      read_status: 'readable',
      description: '',
    };
    expect(validImageUrl(image.url, image.path, { baseUrl, version })).toBe(
      true,
    );
    for (const url of [
      image.url.replace('bidder-1', 'bidder-2'),
      image.url.replace(version, 'b'.repeat(64)),
      `https://evil.test/${image.url}`,
    ]) {
      expect(validImageUrl(url, image.path, { baseUrl, version })).toBe(false);
    }
    const html = safeMarkdown(
      '![图](file-1/images/1.png)<script>alert(1)</script><img src="https://evil.test/x">',
      [image],
      '',
      { baseUrl, version },
    );
    expect(html).toContain('/api/bidders/bidder-1/files/file-1/preview');
    expect(html).not.toMatch(/<script|https:\/\/evil/);
  });

  it('进度页真实终态停止轮询，完成文件与已保存数分开显示', async () => {
    const fetch = vi.fn().mockResolvedValue(
      reply({
        bidder_id: 'bidder-1',
        display_name: '进度库',
        status: 'evidence_failed',
        phase: 'failed',
        failed_phase: 'ingesting',
        document_count: 2,
        completed_file_ids: ['file-1'],
        saved_material_count: 2,
        files: [{ ...source, has_reading_limit: true }],
        recent_materials: [material],
        error: { code: 'test', message: '第二份未完成' },
        warning: null,
        trace: {
          trace_status: 'failed',
          changes: [],
          next_cursor: 0,
          has_more: false,
          warning: null,
        },
      }),
    );
    vi.stubGlobal('fetch', fetch);
    render(
      <App>
        <Progress />
      </App>,
    );
    await screen.findByRole('heading', { name: '进度库' });
    expect(screen.queryByText(/存在读取限制/)).not.toBeInTheDocument();
    expect(screen.getByText('1 / 2')).toBeInTheDocument();
    expect(screen.getByText('第二份未完成')).toBeInTheDocument();
    expect(screen.queryByText('file-1')).not.toBeInTheDocument();
    expect(
      document.querySelector('.ant-card-head [data-entity-id="file-1"]'),
    ).toHaveTextContent('合同.md');
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(
      screen.getByRole('region', { name: '资料 Agent 执行轨迹' }),
    ).toHaveClass('trace-panel-scroll');
    const css = readFileSync('src/global.less', 'utf8');
    expect(css).toMatch(/\.trace-panel-scroll[\s\S]*?overflow: auto/);
  });
});
