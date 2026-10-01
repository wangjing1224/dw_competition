import {
  PauseOutlined,
  PlayCircleOutlined,
  RedoOutlined,
} from '@ant-design/icons';
import { Button, Select, Slider, Tag } from 'antd';
import { replayTime } from './replayModel';
import type { AgentReplay } from './useAgentReplay';

export default function ReplayControls({ replay }: { replay: AgentReplay }) {
  if (!replay.replaying)
    return (
      <section className="audit-replay-toolbar" aria-label="Agent 回放控制">
        <Button
          size="small"
          icon={<PlayCircleOutlined aria-hidden="true" />}
          disabled={!!replay.reason}
          onClick={replay.start}
        >
          回放执行
        </Button>
        <span className="audit-muted">
          {replay.reason || '按当前批次的已记录时间回放，不重新执行审核'}
        </span>
      </section>
    );
  return (
    <section
      className="audit-replay-toolbar is-replaying"
      aria-label="Agent 回放控制"
    >
      <Tag color="blue">
        {replay.ended ? '回放结束' : replay.playing ? '回放中' : '回放已暂停'}
      </Tag>
      <Button
        size="small"
        onClick={replay.toggle}
        icon={
          replay.ended ? (
            <RedoOutlined aria-hidden="true" />
          ) : replay.playing ? (
            <PauseOutlined aria-hidden="true" />
          ) : (
            <PlayCircleOutlined aria-hidden="true" />
          )
        }
      >
        {replay.ended ? '重新播放' : replay.playing ? '暂停回放' : '继续回放'}
      </Button>
      <Slider
        className="audit-replay-slider"
        ariaLabelForHandle="回放进度"
        min={0}
        max={replay.duration}
        step={1}
        value={replay.position}
        onChange={replay.seek}
        tooltip={{ formatter: (value) => replayTime(value || 0) }}
      />
      <span className="audit-replay-time" title="回放时间">
        {replayTime(replay.position)} / {replayTime(replay.duration)}
      </span>
      <Select
        size="small"
        aria-label="回放倍速"
        value={replay.speed}
        onChange={replay.setSpeed}
        options={[0.5, 1, 2, 4, 8].map((value) => ({
          value,
          label: `${value}×`,
        }))}
        style={{ width: 72 }}
      />
      <Button size="small" onClick={replay.exit}>
        回到实时
      </Button>
      <span className="audit-replay-note">
        仅还原图中节点状态与次数；上方进度、轨迹和调用详情仍为最新已保存记录。
      </span>
    </section>
  );
}
