// @vitest-environment jsdom
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { Span } from '@/services/auditTasks';
import TraceTree from './TraceTree';

vi.mock('@umijs/max', () => ({ Link: () => null, history: { push: vi.fn() } }));
afterEach(cleanup);
const span = (id: string, kind: string, parent: string | null): Span => ({
  span_id: id,
  parent_span_id: parent,
  kind,
  title: id,
  name: id,
  actor: 'bid_agent',
  file_id: 'file-1',
  batch_index: 1,
  turn_index: 1,
  tool_call_id: null,
  status: 'completed',
  started_at: '2026-10-01T10:00:00Z',
  finished_at: null,
  duration_ms: 1,
  summary: '',
  has_detail: true,
});
it('轮次和工具默认展开，手动收起不被轮询覆盖，可全部展开/收起', async () => {
  const spans = [
    span('文件', 'file', null),
    span('第一轮', 'turn', '文件'),
    span('检索调用', 'tool', '第一轮'),
  ];
  const onSelect = vi.fn();
  const { rerender } = render(<TraceTree spans={spans} onSelect={onSelect} />);
  expect(screen.getByText('检索调用')).toBeInTheDocument();
  const turn = screen.getByText('第一轮').closest('.ant-tree-treenode');
  const toggle = turn?.querySelector('.ant-tree-switcher');
  if (!toggle) throw new Error('轮次折叠开关缺失');
  fireEvent.click(toggle);
  await waitFor(() =>
    expect(screen.queryByText('检索调用')).not.toBeInTheDocument(),
  );
  rerender(
    <TraceTree
      spans={[...spans, span('后到的调用', 'tool', '第一轮')]}
      onSelect={onSelect}
    />,
  );
  expect(screen.queryByText('后到的调用')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '全部展开' }));
  expect(screen.getByText('后到的调用')).toBeInTheDocument();
  fireEvent.click(screen.getByText('后到的调用'));
  expect(onSelect).toHaveBeenCalledWith('后到的调用');
  fireEvent.click(screen.getByRole('button', { name: '全部收起' }));
  await waitFor(() => {
    expect(screen.queryByText('检索调用')).not.toBeInTheDocument();
    expect(screen.queryByText('第一轮')).not.toBeInTheDocument();
  });
});

it('选中调用会展开祖先，用户收起后，轮询更新不会再强制展开', async () => {
  const spans = [
    span('文件', 'file', null),
    span('轮次', 'turn', '文件'),
    span('模型返回', 'model', '轮次'),
  ];
  const onSelect = vi.fn();
  const { rerender } = render(
    <TraceTree spans={spans} selected="模型返回" onSelect={onSelect} />,
  );
  expect(await screen.findByText('模型返回')).toBeInTheDocument();
  const toggle = screen
    .getByText('轮次')
    .closest('.ant-tree-treenode')
    ?.querySelector('.ant-tree-switcher');
  if (!toggle) throw new Error('缺少真实树展开控件');
  fireEvent.click(toggle);
  await waitFor(() =>
    expect(screen.queryByText('模型返回')).not.toBeInTheDocument(),
  );
  rerender(
    <TraceTree
      spans={spans.map((s) => ({ ...s, duration_ms: 1200 }))}
      selected="模型返回"
      onSelect={onSelect}
    />,
  );
  await waitFor(() =>
    expect(screen.queryByText('模型返回')).not.toBeInTheDocument(),
  );
});
