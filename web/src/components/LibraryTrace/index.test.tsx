// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import EvidenceProgress from '@/pages/EvidenceLibraries/Progress';
import RuleProgress from '@/pages/RuleLibraries/Progress';
import type { TraceNode } from '@/services/ruleLibraries';
import { SourceFileList, TraceDetail, TraceTree } from '.';

vi.mock('@umijs/max', () => ({
  Link: ({ to, children }: { to: string; children: ReactNode }) => (
    <a href={to}>{children}</a>
  ),
  useParams: () => ({ projectId: 'project-1', bidderId: 'bidder-1' }),
}));
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const node: TraceNode = {
  span_id: 'span-image',
  parent_span_id: null,
  kind: 'tool',
  file_id: 'file-1',
  batch_index: 1,
  turn_index: 1,
  name: 'view_image',
  title: '查看合同图片',
  tool_call_id: 'call-image',
  status: 'succeeded',
  started_at: '2026-10-05T00:00:00Z',
  finished_at: '2026-10-05T00:00:01Z',
  duration_ms: 1000,
  summary: '',
  has_detail: true,
};
const detail = {
  node,
  input: { path: 'file-1/images/1.png' },
  output: null,
  error: null,
  truncated: false,
};
const image = {
  path: 'file-1/images/1.png',
  url: '/api/bidders/bidder-1/files/file-1/preview?image_name=1.png&version=abc',
};

describe('规则库与资料库公共来源文件列表', () => {
  it.each([
    '抽取',
    '整理',
  ] as const)('%s 状态共用条目排版、处理中高亮和解析状态', (activity) => {
    const { container } = render(
      <SourceFileList
        activity={activity}
        files={[
          [undefined, 'pending'],
          ['processing', 'partial'],
          ['completed', 'parsed'],
          ['failed', 'unsupported'],
        ].map(([status, parseStatus], index) => ({
          file_id: `file-${index + 1}`,
          file_name: `文件${index + 1}.docx`,
          source_path: `材料/文件${index + 1}.docx`,
          status,
          parse_status: parseStatus as string,
        }))}
      />,
    );
    expect(container.querySelectorAll('.file-progress-list')).toHaveLength(1);
    const files = container.querySelectorAll('.file-progress');
    expect(files).toHaveLength(4);
    expect(
      within(files[0] as HTMLElement).getByText(`等待${activity}`),
    ).toBeInTheDocument();
    expect(files[1]).toHaveClass('active');
    expect(
      within(files[1] as HTMLElement).getByText(`${activity}中`),
    ).toBeInTheDocument();
    expect(within(files[2] as HTMLElement).getByText('已完成')).toHaveClass(
      'ant-tag-green',
    );
    expect(within(files[3] as HTMLElement).getByText('失败')).toHaveClass(
      'ant-tag-red',
    );
    for (const text of ['等待解析', '部分可读', '完成', '不支持']) {
      expect(screen.getByText(`解析：${text}`)).toBeInTheDocument();
    }
    expect(screen.queryByText('file-1')).not.toBeInTheDocument();
    expect(screen.getByText('文件1.docx').closest('strong')).not.toBeNull();
  });

  it.each([
    [
      '规则库',
      RuleProgress,
      '/api/projects/project-1',
      'rule_ready',
      '来源文件列表',
    ],
    [
      '资料库',
      EvidenceProgress,
      '/api/bidders/bidder-1',
      'evidence_ready',
      '资料文件列表',
    ],
  ] as const)('%s 处理页复用来源列表，资料长文件名左对齐换行且预览仍可用', async (_, Progress, base, status, label) => {
    const file = {
      file_id: 'file-1',
      file_name: '技术人员及项目经理相关资质与证明材料（附件一）.docx',
      source_path:
        '人员资格/技术人员及项目经理相关资质与证明材料（附件一）.docx',
      parse_status: 'partial',
      extraction_status: 'completed',
      ingestion_status: 'completed',
    };
    const fetch = vi.fn(async (url: string) => ({
      ok: true,
      json: async () =>
        url.endsWith('/preview')
          ? {
              ...file,
              parse_version: 'v1',
              markdown: '资料解析正文',
              warnings: [],
              images: [],
            }
          : url.includes('/progress?')
            ? {
                display_name: '文件展示测试',
                status,
                phase: 'completed',
                document_count: 1,
                completed_file_ids: [file.file_id],
                saved_rule_count: 0,
                saved_material_count: 1,
                files: [file],
                recent_rules: [],
                recent_materials: [],
                trace: {
                  changes: [],
                  next_cursor: 0,
                  trace_status: 'completed',
                  has_more: false,
                },
              }
            : { rule_set: { rules: [] } },
    }));
    vi.stubGlobal('fetch', fetch);
    render(
      <>
        <style>{readFileSync('src/global.less', 'utf8')}</style>
        <Progress />
      </>,
    );
    await screen.findByText(file.file_name);
    const panel = screen.getByRole('region', { name: label });
    expect(panel).toHaveClass('trace-panel-scroll');
    expect(
      panel.querySelectorAll('.file-progress-list > .file-progress'),
    ).toHaveLength(1);
    expect(within(panel).getByText(file.source_path)).toBeInTheDocument();
    expect(within(panel).getByText('已完成')).toBeInTheDocument();
    expect(within(panel).getByText('解析：部分可读')).toBeInTheDocument();
    expect(
      within(panel).getByText(file.file_name).closest('strong'),
    ).not.toBeNull();
    if (base.startsWith('/api/bidders')) {
      const button = within(panel).getByRole('button', {
        name: file.file_name,
      });
      expect(button).toHaveAttribute('type', 'button');
      // jsdom 的原生 button 默认居中会覆盖普通作者声明；检查实际匹配的 CSS 规则，
      // 不为测试给生产样式添加 !important，也不将这里当作浏览器像素验证。
      const nameRule = Array.from(document.styleSheets)
        .flatMap((sheet) => Array.from(sheet.cssRules))
        .find(
          (rule) =>
            rule instanceof CSSStyleRule &&
            rule.selectorText === '.file-progress-name',
        ) as CSSStyleRule;
      expect(button.matches(nameRule.selectorText)).toBe(true);
      expect(nameRule.style.getPropertyValue('text-align')).toBe('left');
      expect(getComputedStyle(button).whiteSpace).toBe('normal');
      expect(getComputedStyle(button).overflowWrap).toBe('anywhere');
      expect(getComputedStyle(button).padding).toBe('0px');
      fireEvent.click(button);
      const preview = await screen.findByRole('dialog');
      expect(
        await within(preview).findByText('资料解析正文'),
      ).toBeInTheDocument();
      expect(
        fetch.mock.calls.some(
          ([url]) => url === `${base}/files/file-1/preview`,
        ),
      ).toBe(true);
    }
  });
});

describe('规则库与资料库公共轨迹展示', () => {
  it.each([
    'succeeded',
    'running',
    'failed',
    'partial',
    'interrupted',
  ])('%s 状态图标、展开箭头和带留白的节点文字垂直居中，整行仍可点击', (status) => {
    const onSelect = vi.fn();
    const { container } = render(
      <>
        <style>{readFileSync('src/global.less', 'utf8')}</style>
        <TraceTree
          nodes={{ [node.span_id]: { seq: 1, node: { ...node, status } } }}
          onExpand={vi.fn()}
          onSelect={onSelect}
        />
      </>,
    );
    // jsdom 不计算几何坐标；验证实际 Ant Tree DOM 的对齐样式，防止只修标题而未修图标。
    expect(container.querySelector('.library-trace-tree')).not.toBeNull();
    // Tree 另有测量用的隐藏行，这里定位真实节点。
    const content = screen
      .getByText(node.title)
      .closest('.ant-tree-node-content-wrapper') as HTMLElement;
    const row = content.closest('.ant-tree-treenode') as HTMLElement;
    const icon = content.querySelector('.ant-tree-iconEle') as HTMLElement;
    const switcher = row.querySelector('.ant-tree-switcher') as HTMLElement;
    expect(getComputedStyle(row).alignItems).toBe('center');
    expect(getComputedStyle(content).display).toBe('flex');
    expect(getComputedStyle(content).alignItems).toBe('center');
    expect(getComputedStyle(icon).display).toBe('inline-flex');
    expect(getComputedStyle(icon).alignItems).toBe('center');
    expect(getComputedStyle(icon).flexShrink).toBe('0');
    expect(getComputedStyle(switcher).alignSelf).toBe('center');
    fireEvent.click(content);
    expect(onSelect).toHaveBeenCalledWith(node.span_id);
  });

  it.each([
    '检索到合同正文',
    { content: '检索到合同正文' },
  ])('两类输出形态统一展示正文，不露出 JSON 包装', (output) => {
    render(<TraceDetail detail={{ ...detail, output }} />);
    expect(screen.getByText('已完成')).toBeInTheDocument();
    expect(screen.getByText('call-image')).toBeInTheDocument();
    expect(screen.getByText('检索到合同正文')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '输入' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '输出' })).toBeInTheDocument();
    expect(screen.queryByText(/"content"/)).not.toBeInTheDocument();
  });

  it('图片结果展示图片，同时保留伴随正文，不展示图片 URL 的 JSON', () => {
    render(
      <TraceDetail
        detail={{
          ...detail,
          output: { content: '已读取合同首页', images: [image] },
        }}
      />,
    );
    expect(screen.getByRole('img', { name: image.path })).toHaveAttribute(
      'src',
      image.url,
    );
    expect(screen.getByText('已读取合同首页')).toBeInTheDocument();
    expect(screen.queryByText(/"images"/)).not.toBeInTheDocument();
  });

  it('各自的规则变更和资料前后对照不因统一展示而丢失', () => {
    const { rerender } = render(
      <TraceDetail
        detail={{
          ...detail,
          output: {
            content: '保存成功',
            rule_changes: [
              { before: null, after: { requirement: '提交资质证明' } },
            ],
          },
        }}
      />,
    );
    expect(
      screen.getByRole('heading', { name: '已提交规则变更' }),
    ).toBeInTheDocument();
    expect(screen.getByText(/提交资质证明/)).toBeInTheDocument();
    rerender(
      <TraceDetail
        detail={{
          ...detail,
          output: '保存成功',
          material_change: {
            before: { bid_summary: '修改前摘要' },
            after: { bid_summary: '修改后摘要' },
          },
        }}
      />,
    );
    expect(
      screen.getByRole('heading', { name: '已保存变更（修改前 → 修改后）' }),
    ).toBeInTheDocument();
    expect(screen.getByText(/修改前摘要/)).toHaveTextContent('修改后摘要');
    expect(
      screen.queryByRole('heading', { name: '已提交规则变更' }),
    ).not.toBeInTheDocument();
  });

  it('错误、截断提示和无输入/等待返回统一显示', () => {
    render(
      <TraceDetail
        detail={{
          ...detail,
          node: { ...node, status: 'failed', tool_call_id: null },
          input: null,
          truncated: true,
          error: { message: '读取失败' },
        }}
      />,
    );
    expect(screen.getByText('失败')).toBeInTheDocument();
    expect(screen.getByText('读取失败')).toBeInTheDocument();
    expect(
      screen.getByText('长内容仅展示前部，实际调用内容未改变'),
    ).toBeInTheDocument();
    expect(screen.getByText('无')).toBeInTheDocument();
    expect(screen.getByText('等待返回')).toBeInTheDocument();
  });

  it.each([
    ['规则库', RuleProgress, '/api/projects/project-1', 'rule_ready'],
    ['资料库', EvidenceProgress, '/api/bidders/bidder-1', 'evidence_ready'],
  ] as const)('%s 点击节点使用相同详情展示，仍请求各自接口', async (_, Progress, base, status) => {
    const imageUrl = `${base}/files/file-1/preview?image_name=1.png&version=abc`;
    const fetch = vi.fn(async (url: string) => ({
      ok: true,
      json: async () =>
        url.includes('/trace/')
          ? { ...detail, output: { images: [{ ...image, url: imageUrl }] } }
          : url.includes('/progress?')
            ? {
                display_name: '测试库',
                status,
                phase: 'completed',
                document_count: 0,
                completed_file_ids: [],
                saved_rule_count: 0,
                saved_material_count: 0,
                files: [],
                recent_rules: [],
                recent_materials: [],
                trace: {
                  changes: [{ seq: 1, node }],
                  next_cursor: 1,
                  trace_status: 'completed',
                  has_more: false,
                },
              }
            : { rule_set: { rules: [] } },
    }));
    vi.stubGlobal('fetch', fetch);
    render(<Progress />);
    fireEvent.click(await screen.findByText(node.title));
    const dialog = await screen.findByRole('dialog');
    expect(
      await within(dialog).findByRole('img', { name: image.path }),
    ).toHaveAttribute('src', imageUrl);
    expect(within(dialog).getByText('已完成')).toBeInTheDocument();
    expect(
      within(dialog).getByText(node.tool_call_id as string),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole('heading', { name: '输入' }),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole('heading', { name: '输出' }),
    ).toBeInTheDocument();
    expect(
      fetch.mock.calls.some(([url]) => url === `${base}/trace/${node.span_id}`),
    ).toBe(true);
  });
});
