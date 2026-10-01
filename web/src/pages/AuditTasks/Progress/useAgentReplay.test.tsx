import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { Span } from '@/services/auditTasks';
import { agentGraph } from './graphModel';
import useAgentReplay from './useAgentReplay';

const date = (ms: number) => new Date(Date.UTC(2026, 9, 1) + ms).toISOString();
const span = (
  id: string,
  start: number,
  end: number,
  extra: Partial<Span> = {},
): Span => ({
  span_id: id,
  kind: 'model',
  parent_span_id: 'main',
  actor: 'bid_agent',
  file_id: 'f',
  name: 'model',
  title: '模型调用',
  status: 'completed',
  started_at: date(start),
  finished_at: date(end),
  duration_ms: end - start,
  batch_index: 1,
  turn_index: 1,
  tool_call_id: null,
  summary: '',
  has_detail: true,
  ...extra,
});
const spans = [
  span('main', 0, 5000, { kind: 'batch', parent_span_id: null }),
  span('model1', 100, 1500),
  span('search', 1500, 3000, { kind: 'tool', tool_name: 'search_documents' }),
  span('model2', 3000, 4900),
];
beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});
const setup = () => {
  const onStart = vi.fn();
  const props = { active: true, loading: false, spans, session: 'main' };
  const hook = renderHook(
    (p) =>
      useAgentReplay({
        ...p,
        live: agentGraph(p.spans, p.session),
        onStart,
      }),
    { initialProps: props },
  );
  return { ...hook, props, onStart };
};

it('播放、暂停、倍速、拖动、结束重播和回到实时', () => {
  const { result, onStart } = setup();
  expect(result.current.replaying).toBe(false);
  act(() => result.current.start());
  expect(onStart).toHaveBeenCalledWith('main');
  act(() => vi.advanceTimersByTime(1000));
  expect(result.current.position).toBe(1000);
  expect(
    result.current.graph.nodes.find((n) => n.id === 'main-agent')?.status,
  ).toBe('running');
  act(() => result.current.toggle());
  act(() => vi.advanceTimersByTime(1000));
  expect(result.current.position).toBe(1000);
  act(() => {
    result.current.setSpeed(2);
    result.current.toggle();
  });
  act(() => vi.advanceTimersByTime(1000));
  expect(result.current.position).toBe(3000);
  act(() => result.current.seek(2200));
  expect(result.current.playing).toBe(false);
  expect(
    result.current.graph.nodes.find((n) => n.id === 'main-search')?.status,
  ).toBe('running');
  act(() => result.current.toggle());
  act(() => vi.advanceTimersByTime(2000));
  expect(result.current.position).toBe(5000);
  expect(result.current.ended).toBe(true);
  expect(result.current.playing).toBe(false);
  act(() => result.current.toggle());
  expect(result.current.position).toBe(0);
  expect(result.current.playing).toBe(true);
  act(() => result.current.exit());
  expect(result.current.replaying).toBe(false);
  expect(result.current.graph.nodes).toEqual(agentGraph(spans, 'main').nodes);
  expect(vi.getTimerCount()).toBe(0);
});
it('后台记录更新不改变本次回放快照，退出后恢复最新数据', () => {
  const { result, rerender, props } = setup();
  act(() => result.current.start());
  act(() => result.current.seek(4000));
  const updated = [
    ...spans,
    span('added', 4000, 4800, { kind: 'tool', tool_name: 'view_image' }),
  ];
  rerender({ ...props, spans: updated });
  expect(
    result.current.graph.nodes.find((n) => n.id === 'main-image')?.members,
  ).toHaveLength(0);
  act(() => result.current.exit());
  expect(
    result.current.graph.nodes.find((n) => n.id === 'main-image')?.members,
  ).toEqual(['added']);
});
it('切视图、切批次、卸载停止计时，不残留旧回放', () => {
  const { result, rerender, props, unmount } = setup();
  act(() => result.current.start());
  rerender({ ...props, active: false });
  expect(result.current.replaying).toBe(false);
  expect(vi.getTimerCount()).toBe(0);
  rerender(props);
  act(() => result.current.start());
  rerender({
    ...props,
    session: 'other',
    spans: [
      ...spans,
      span('other', 6000, 8000, { kind: 'batch', parent_span_id: null }),
    ],
  });
  expect(result.current.replaying).toBe(false);
  expect(vi.getTimerCount()).toBe(0);
  rerender(props);
  act(() => result.current.start());
  unmount();
  expect(vi.getTimerCount()).toBe(0);
});
it('加载中和未结束批次不能启动回放', () => {
  const { result, rerender, props } = setup();
  rerender({ ...props, loading: true });
  expect(result.current.reason).toContain('加载完成');
  act(() => result.current.start());
  expect(result.current.replaying).toBe(false);
  rerender({
    ...props,
    spans: spans.map((s) =>
      s.span_id === 'main' ? { ...s, status: 'running' } : s,
    ),
  });
  expect(result.current.reason).toContain('结束后');
  act(() => result.current.start());
  expect(result.current.replaying).toBe(false);
});
