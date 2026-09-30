import { Alert, Button, Drawer, Segmented, Table } from 'antd';
import { useState } from 'react';
import type { Span } from '@/services/auditTasks';
import { Status } from '../shared';
import { actors, duration, failure, spanTitle } from './graphModel';

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
  const events = [...spans]
    .reverse()
    .filter(
      (s) =>
        filter === 'all' ||
        (filter === 'running' ? s.status === 'running' : failure(s.status)),
    );
  return (
    <Drawer title="执行记录" open={open} onClose={onClose} size={720}>
      <p className="audit-muted">
        已加载 {spans.length} 个独立节点的最新状态，不是原始状态变更日志。
      </p>
      {partial && (
        <Alert
          type="warning"
          title="记录尚未加载完整或观测不完整；筛选仅限当前已加载记录"
        />
      )}
      <Segmented
        value={filter}
        onChange={setFilter}
        options={[
          { label: '全部', value: 'all' },
          { label: '进行中', value: 'running' },
          { label: '异常', value: 'errors' },
        ]}
      />
      <Table<Span>
        className="section-gap"
        size="small"
        rowKey="span_id"
        dataSource={events}
        pagination={{ pageSize: 10, showSizeChanger: false }}
        scroll={{ x: 620 }}
        columns={[
          {
            title: '时间',
            render: (_, s) => new Date(s.started_at).toLocaleTimeString(),
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
