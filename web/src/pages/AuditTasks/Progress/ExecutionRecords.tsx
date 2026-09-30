import { Alert, Button, Drawer, Segmented, Select, Space, Table } from 'antd';
import { useState } from 'react';
import type { Span } from '@/services/auditTasks';
import { Status } from '../shared';
import { actors, duration, failure, spanTitle } from './graphModel';

const recordDate = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
});
const recordTime = new Intl.DateTimeFormat('zh-CN', {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
});

export default function ExecutionRecords({
  open,
  onClose,
  spans,
  partial,
  onSelect,
}: {
  open: boolean;
  onClose: () => void;
  spans: Span[];
  partial: boolean;
  onSelect: (id: string) => void;
}) {
  const [filter, setFilter] = useState('all');
  const [selectedDay, setSelectedDay] = useState<string>();
  // 按浏览器本地日期分组；顶部给出日期上下文，行内只保留时间。
  const rows = spans.map((span) => {
    const date = new Date(span.started_at);
    const valid = !Number.isNaN(date.getTime());
    return {
      span,
      day: valid
        ? `${date.getFullYear()}/${String(date.getMonth() + 1).padStart(2, '0')}/${String(date.getDate()).padStart(2, '0')}`
        : '',
      label: valid ? recordDate.format(date) : '日期未记录',
    };
  });
  const dates = [...new Map(rows.map((row) => [row.day, row.label]))].sort(
    ([a], [b]) => b.localeCompare(a),
  );
  const day = dates.some(([key]) => key === selectedDay)
    ? selectedDay
    : dates[0]?.[0];
  const events = rows
    .reverse()
    .filter((row) => row.day === day)
    .map((row) => row.span)
    .filter(
      (s) =>
        filter === 'all' ||
        (filter === 'running' ? s.status === 'running' : failure(s.status)),
    );
  return (
    <Drawer title="执行记录" open={open} onClose={onClose} size={720}>
      <p className="audit-muted">
        已加载 {spans.length}{' '}
        个独立节点的最新状态，不是原始状态变更日志。时间按本地时区显示。
      </p>
      {partial && (
        <Alert
          type="warning"
          title="记录尚未加载完整或观测不完整；筛选仅限当前已加载记录"
        />
      )}
      <div className="audit-record-toolbar">
        <Space>
          <span>日期</span>
          {dates.length > 1 ? (
            <Select
              aria-label="记录日期"
              value={day}
              onChange={setSelectedDay}
              options={dates.map(([value, label]) => ({ value, label }))}
              style={{ width: 140 }}
            />
          ) : (
            <strong>{dates[0]?.[1] || '暂无记录'}</strong>
          )}
        </Space>
        <Segmented
          value={filter}
          onChange={setFilter}
          options={[
            { label: '全部', value: 'all' },
            { label: '进行中', value: 'running' },
            { label: '异常', value: 'errors' },
          ]}
        />
      </div>
      <Table<Span>
        className="section-gap"
        size="small"
        rowKey="span_id"
        dataSource={events}
        pagination={{ pageSize: 10, showSizeChanger: false }}
        scroll={{ x: 620 }}
        columns={[
          {
            title: '开始时间',
            width: 100,
            render: (_, s) => {
              const date = new Date(s.started_at);
              return Number.isNaN(date.getTime()) ? (
                '未记录'
              ) : (
                <time className="audit-record-time" dateTime={s.started_at}>
                  {recordTime.format(date)}
                </time>
              );
            },
          },
          { title: '执行方', render: (_, s) => actors[s.actor] || s.actor },
          {
            title: '动作',
            render: (_, s) => (
              <Button
                type="link"
                onClick={() => {
                  onClose();
                  onSelect(s.span_id);
                }}
              >
                {spanTitle(s, spans)}
              </Button>
            ),
          },
          { title: '状态', render: (_, s) => <Status value={s.status} /> },
          { title: '耗时', render: (_, s) => duration(s) },
        ]}
      />
    </Drawer>
  );
}
