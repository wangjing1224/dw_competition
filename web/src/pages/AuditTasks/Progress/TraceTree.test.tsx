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
  file_id: 'file-1',
  parent_span_id: parent,
  actor: 'bid_agent',
  name: id,
  title: id,
  kind,
  status: 'running',
  duration_ms: null,
  has_detail: false,
  started_at: '2026-09-30T10:00:00Z',
  finished_at: null,
  batch_index: null,
  turn_index: null,
  tool_call_id: null,
  summary: '',
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
      spans={spans.map((s) => ({
        ...s,
        status: 'completed',
        duration_ms: 1200,
      }))}
      selected="模型返回"
      onSelect={onSelect}
    />,
  );
  await waitFor(() =>
    expect(screen.queryByText('模型返回')).not.toBeInTheDocument(),
  );
});
