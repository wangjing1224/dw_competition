import { useEffect, useMemo, useState } from 'react';
import type { Span } from '@/services/auditTasks';
import type { agentGraph } from './graphModel';
import {
  buildReplayTimeline,
  type ReplayTimeline,
  replayGraph,
} from './replayModel';

export default function useAgentReplay({
  spans,
  live,
  active,
  loading,
  onStart,
}: {
  spans: Span[];
  live: ReturnType<typeof agentGraph>;
  active: boolean;
  loading: boolean;
  onStart: (sessionId: string) => void;
}) {
  const candidate = useMemo(
    () => buildReplayTimeline(spans, live.session?.span_id),
    [spans, live.session?.span_id],
  );
  const [snapshot, setSnapshot] = useState<ReplayTimeline>();
  const [position, setPosition] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const replaying =
    !!snapshot && active && snapshot.sessionId === live.session?.span_id;
  const duration = snapshot?.duration || 0;
  const ended = replaying && position >= duration;
  const reason = loading ? '轨迹加载完成后可回放' : candidate.reason;
  // 切视图/批次停止回放；切文件由外层 key 卸载，计时器不会串到另一文件。
  useEffect(() => {
    if (!replaying) {
      setSnapshot(undefined);
      setPlaying(false);
      setPosition(0);
    }
  }, [replaying]);
  useEffect(() => {
    if (!replaying || !playing || ended) return;
    let last = performance.now();
    const timer = setInterval(() => {
      const now = performance.now();
      const elapsed = Math.max(0, now - last);
      last = now;
      setPosition((value) => Math.min(duration, value + elapsed * speed));
    }, 50);
    return () => clearInterval(timer);
  }, [replaying, playing, ended, duration, speed]);
  useEffect(() => {
    if (ended) setPlaying(false);
  }, [ended]);
  const graph = useMemo(
    () => (replaying && snapshot ? replayGraph(snapshot, position) : live),
    [replaying, snapshot, position, live],
  );
  return {
    graph,
    replaying,
    playing: playing && replaying && !ended,
    ended,
    position,
    duration,
    speed,
    reason,
    start: () => {
      if (!active || loading || !candidate.timeline) return;
      // 冻结这次回放的数据，后台继续轮询不会把进度条拉长或跳到下一批。
      setSnapshot(candidate.timeline);
      setPosition(0);
      setPlaying(true);
      onStart(candidate.timeline.sessionId);
    },
    toggle: () => {
      if (ended) setPosition(0);
      setPlaying((value) => ended || !value);
    },
    seek: (value: number) => {
      setPlaying(false);
      setPosition(Math.max(0, Math.min(duration, value)));
    },
    setSpeed,
    exit: () => {
      setPlaying(false);
      setSnapshot(undefined);
      setPosition(0);
    },
  };
}
export type AgentReplay = ReturnType<typeof useAgentReplay>;
