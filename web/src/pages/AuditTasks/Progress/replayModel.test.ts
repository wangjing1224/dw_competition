import { describe, expect, it } from 'vitest';
import type { Span } from '@/services/auditTasks';
import { agentGraph } from './graphModel';
import { buildReplayTimeline, replayGraph, replayTime } from './replayModel';

const date = (ms: number) => new Date(Date.UTC(2026, 9, 1) + ms).toISOString();
const span = (
  id: string,
  kind: string,
  parent: string | null,
  start: number,
  end: number,
  extra: Partial<Span> = {},
): Span => ({
  span_id: id,
  kind,
  parent_span_id: parent,
  actor: 'bid_agent',
  file_id: 'file-1',
  batch_index: 1,
  turn_index: null,
  tool_call_id: null,
  name: kind,
  title: kind,
  status: 'completed',
  started_at: date(start),
  finished_at: date(end),
  duration_ms: end - start,
  summary: '已保存结果',
  has_detail: true,
  ...extra,
});
const sample = () => [
  span('file', 'file', null, 0, 12000, { actor: 'system' }),
  span('main', 'batch', 'file', 0, 10000),
  span('model1', 'model', 'main', 100, 2000),
  span('search', 'tool', 'main', 2000, 3000, {
    tool_name: 'search_documents',
    status: 'failed',
  }),
  span('model2', 'model', 'main', 3000, 4000),
  span('delegate', 'tool', 'main', 4000, 9000, {
    tool_name: 'request_evidence_review',
  }),
  span('check', 'evidence_check', 'delegate', 4000, 9000, {
    actor: 'evidence_subagent',
  }),
  span('child', 'batch', 'check', 4000, 9000, { actor: 'evidence_subagent' }),
  span('child-model', 'model', 'child', 4100, 5000, {
    actor: 'evidence_subagent',
  }),
  span('child-search', 'tool', 'child', 5000, 7000, {
    actor: 'evidence_subagent',
    tool_name: 'search_documents',
  }),
  span('child-model2', 'model', 'child', 7000, 8900, {
    actor: 'evidence_subagent',
  }),
  span('model3', 'model', 'main', 9000, 10000),
  span('next', 'batch', 'file', 11000, 12000),
  span('next-model', 'model', 'next', 11000, 12000),
];
const timeline = (spans = sample()) => {
  const result = buildReplayTimeline(spans, 'main');
  if (!result.timeline) throw new Error(result.reason);
  return result.timeline;
};
const running = (offset: number) =>
  replayGraph(timeline(), offset)
    .nodes.filter((n) => n.status === 'running')
    .map((n) => n.id);

describe('Agent 历史回放区间', () => {
  it('只回放所选会话，包含真实祖先；按墙钟时间而不是嵌套调用耗时之和', () => {
    const data = timeline();
    expect(data.duration).toBe(10000);
    expect(data.context.map((s) => s.span_id)).toEqual(['file']);
    expect(data.entries.some((e) => e.span.span_id === 'next-model')).toBe(
      false,
    );
    expect(replayGraph(data, 10000).nodes).toEqual(
      agentGraph(sample(), 'main').nodes,
    );
  });
  it('主模型、工具、委派、子模型和子工具按真实起止时间切换', () => {
    expect(running(0)).toEqual([]);
    expect(running(100)).toEqual(['main-agent']);
    expect(running(2000)).toEqual(['main-search']);
    expect(running(3500)).toEqual(['main-agent']);
    expect(running(4200)).toEqual(['delegate', 'evidence-agent']);
    expect(running(6000)).toEqual(['delegate', 'evidence-search']);
    expect(running(7500)).toEqual(['delegate', 'evidence-agent']);
    expect(running(9500)).toEqual(['main-agent']);
    expect(running(10000)).toEqual([]);
  });
  it('未来调用不提前计数，失败只在结束后出现，拖回前面可重新还原', () => {
    const data = timeline();
    const first = replayGraph(data, 1000);
    expect(first.nodes.find((n) => n.id === 'main-agent')?.subtitle).toBe(
      '模型调用 1 次',
    );
    expect(first.nodes.find((n) => n.id === 'main-search')?.members).toEqual(
      [],
    );
    expect(
      replayGraph(data, 2500).nodes.find((n) => n.id === 'main-search')?.status,
    ).toBe('running');
    expect(
      replayGraph(data, 3000).nodes.find((n) => n.id === 'main-search')
        ?.subtitle,
    ).toContain('曾失败 1 次');
    expect(
      replayGraph(data, 10000).nodes.find((n) => n.id === 'main-agent')
        ?.subtitle,
    ).toBe('模型调用 3 次');
    expect(replayGraph(data, 1000).nodes).toEqual(first.nodes);
    expect(first.nodes.map((n) => [n.id, n.x, n.y])).toEqual(
      replayGraph(data, 9000).nodes.map((n) => [n.id, n.x, n.y]),
    );
  });
  it('不修改原始 span，负值/超长进度夹到边界', () => {
    const spans = sample();
    const before = JSON.stringify(spans);
    const data = timeline(spans);
    expect(replayGraph(data, -10).nodes).toEqual(replayGraph(data, 0).nodes);
    expect(replayGraph(data, 20000).nodes).toEqual(
      replayGraph(data, 10000).nodes,
    );
    expect(JSON.stringify(spans)).toBe(before);
  });
  it('运行中、空轨迹和损坏时间不给出伪回放', () => {
    expect(buildReplayTimeline([]).timeline).toBeUndefined();
    expect(
      buildReplayTimeline([
        span('main', 'batch', null, 0, 10000, { status: 'running' }),
      ]).reason,
    ).toContain('结束后');
    expect(
      buildReplayTimeline([span('main', 'batch', null, 0, 10000)]).reason,
    ).toContain('暂无可回放');
    for (const change of [
      { started_at: 'invalid' },
      { finished_at: date(1) },
      { finished_at: null, duration_ms: null },
      { finished_at: date(11000) },
    ]) {
      const spans = sample().map((s) =>
        s.span_id === 'search' ? { ...s, ...change } : s,
      );
      expect(buildReplayTimeline(spans, 'main').timeline).toBeUndefined();
    }
  });
  it('缺结束时间可使用真实 duration，不捏造零毫秒调用的运行时长', () => {
    const spans = sample().map((s) =>
      s.span_id === 'search' ? { ...s, finished_at: null } : s,
    );
    expect(
      timeline(spans).entries.find((e) => e.span.span_id === 'search')?.end,
    ).toBe(Date.parse(date(3000)));
    const zero = timeline(
      sample().map((s) =>
        s.span_id === 'search'
          ? { ...s, finished_at: s.started_at, duration_ms: 0 }
          : s,
      ),
    );
    expect(
      replayGraph(zero, 2000).nodes.find((n) => n.id === 'main-search')?.status,
    ).toBe('failed');
  });
  it('父节点缺失的记录不串进当前会话，区间超过父调用则拒绝', () => {
    const disconnected = sample().filter((s) => s.span_id !== 'delegate');
    expect(
      timeline(disconnected).entries.some(
        (e) => e.span.actor === 'evidence_subagent',
      ),
    ).toBe(false);
    const inconsistent = sample().map((s) =>
      s.span_id === 'child-search' ? { ...s, finished_at: date(9500) } : s,
    );
    expect(buildReplayTimeline(inconsistent, 'main').timeline).toBeUndefined();
  });
  it('时间显示支持分钟、小时和亚秒', () => {
    expect(replayTime(1500)).toBe('00:01.5');
    expect(replayTime(61000)).toBe('01:01.0');
    expect(replayTime(3600000)).toBe('1:00:00.0');
  });
});
