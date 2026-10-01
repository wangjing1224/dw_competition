import type { Span } from '@/services/auditTasks';
import { agentGraph, ancestors, traceForest } from './graphModel';

export type ReplayTimeline = {
  sessionId: string;
  start: number;
  duration: number;
  context: Span[];
  entries: { span: Span; start: number; end: number }[];
};

function interval(span: Span) {
  const start = Date.parse(span.started_at);
  const stamp = span.finished_at || span.ended_at;
  const end = stamp
    ? Date.parse(stamp)
    : span.duration_ms != null && Number.isFinite(span.duration_ms)
      ? start + span.duration_ms
      : NaN;
  if (
    !Number.isFinite(start) ||
    !Number.isFinite(end) ||
    end < start ||
    ['running', 'queued', 'waiting'].includes(span.status)
  )
    return null;
  return { start, end };
}

/** 从已结束 span 的区间还原节点状态；不是重放模型/工具，也不改原始记录。 */
export function buildReplayTimeline(
  spans: Span[],
  sessionId?: string,
): { timeline?: ReplayTimeline; reason?: string } {
  const graph = agentGraph(spans, sessionId);
  const session = graph.session;
  if (!session) return { reason: '暂无可回放的审核批次' };
  if (['running', 'queued', 'waiting'].includes(session.status))
    return { reason: '当前批次结束后可回放' };
  const range = interval(session);
  if (!range || range.end <= range.start)
    return { reason: '该批次缺少可用的起止时间，无法回放' };
  const { byId, detached } = traceForest(spans);
  const detachedIds = new Set(detached.map((s) => s.span_id));
  const scope = spans.filter(
    (s) =>
      !detachedIds.has(s.span_id) &&
      (s === session || ancestors(s, byId).includes(session)),
  );
  if (!scope.some((s) => ['model', 'tool'].includes(s.kind)))
    return { reason: '暂无可回放的模型或工具调用' };
  const entries: ReplayTimeline['entries'] = [];
  for (const span of scope) {
    const time = interval(span);
    if (!time || time.start < range.start || time.end > range.end)
      return { reason: '调用时间记录不完整或不一致，无法回放' };
    const parent = byId.get(span.parent_span_id || '');
    // 缺失/冲突的区间不能通过排列顺序补造。
    if (span !== session && parent) {
      const parentTime = interval(parent);
      if (
        !parentTime ||
        time.start < parentTime.start ||
        time.end > parentTime.end
      )
        return { reason: '调用时间记录不完整或不一致，无法回放' };
    }
    entries.push({ span: { ...span }, ...time });
  }
  return {
    timeline: {
      sessionId: session.span_id,
      start: range.start,
      duration: range.end - range.start,
      context: ancestors(session, byId).map((s) => ({ ...s })),
      entries,
    },
  };
}

export function replayGraph(timeline: ReplayTimeline, offset: number) {
  const time =
    timeline.start + Math.max(0, Math.min(timeline.duration, offset));
  const spans = timeline.entries.flatMap(({ span, start, end }) => {
    if (start > time) return [];
    return [
      {
        ...span,
        ...(time < end
          ? {
              status: 'running',
              finished_at: null,
              ended_at: null,
              duration_ms: null,
              summary: '',
            }
          : {}),
      },
    ];
  });
  return agentGraph([...timeline.context, ...spans], timeline.sessionId);
}

export function replayTime(ms: number) {
  const tenths = Math.floor(Math.max(0, ms) / 100);
  const seconds = Math.floor(tenths / 10);
  const minutes = Math.floor(seconds / 60);
  return `${minutes >= 60 ? `${Math.floor(minutes / 60)}:` : ''}${String(minutes % 60).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}.${tenths % 10}`;
}
