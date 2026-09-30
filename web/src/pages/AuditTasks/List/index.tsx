import EntityLabel from '@/components/EntityLabel';
import { Link } from '@umijs/max';
import {
  Button,
  Card,
  Empty,
  Input,
  Pagination,
  Progress,
  Segmented,
  Space,
  Table,
  Spin,
  Alert,
} from 'antd';
import { PlusOutlined, ArrowRightOutlined } from '@ant-design/icons';
import { useState } from 'react';
import { PageHeading, Failure } from '@/pages/RuleLibraries/shared';
import { dateText } from '@/services/ruleLibraries';
import { api, fileUrl } from '@/services/auditTasks';
import type {
  AuditFile,
  Batch,
  TaskList,
  TaskDetail,
} from '@/services/auditTasks';
import { useRemote, Status, StageMatrix, Binding, Download } from '../shared';

function Matrix({ id }: { id: string }) {
  const { data, error, reload } = useRemote(
    (s) => api<TaskDetail>(`/${id}/progress`, { signal: s }),
    [id],
    (d) => d.task.status === 'running',
  );
  const [selected, setSelected] = useState<string>();
  const file: AuditFile | undefined =
    data?.files.find((f) => f.file_id === selected) || data?.files[0];
  return (
    <Card
      title="文件处理矩阵"
      extra={
        <Link to={`/audit-tasks/${id}/progress`}>
          执行工作区 <ArrowRightOutlined />
        </Link>
      }
    >
      {error && <Failure error={error} retry={reload} />}
      {!data && !error ? (
        <Spin />
      ) : (
        data && (
          <>
            {data.task.legacy && (
              <Alert type="info" title="历史任务仅保留导出，不支持逐文件观测" />
            )}
            <StageMatrix
              files={data.files}
              selected={file?.file_id}
              onSelect={(f) => setSelected(f.file_id)}
            />
            <div className="audit-file-context">
              {file ? (
                <>
                  <strong>{file.file_name}</strong>
                  <p>{file.source_path}</p>
                  <Binding file={file} />
                  <p>
                    {file.report_kind === 'final'
                      ? '正式报告已就绪'
                      : file.report_kind === 'draft'
                        ? '已保存报告草稿，尚未定稿'
                        : '尚未生成报告'}
                  </p>
                  <Space>
                    <Link to={fileUrl(id, file.file_id, 'progress')}>
                      查看执行过程
                    </Link>
                    {file.report_kind === 'final' && (
                      <Link to={fileUrl(id, file.file_id)}>查看报告</Link>
                    )}
                  </Space>
                </>
              ) : (
                <Empty description="暂无文件明细" />
              )}
            </div>
            <Download
              url={data.task.outputs.batch_json_url}
              label="导出任务结果"
              name={`${id}.json`}
            />
          </>
        )
      )}
    </Card>
  );
}
export default function TaskListPage() {
  const [group, setGroup] = useState('all');
  const [q, setQ] = useState('');
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<string>();
  const { data, error, reload } = useRemote(
    (s) =>
      api<TaskList>(
        `?${new URLSearchParams({ group, q, page: String(page), page_size: '10' })}`,
        { signal: s },
      ),
    [group, q, page],
    () => true,
  );
  const id = data?.items.some((t) => t.task_id === selected)
    ? selected
    : data?.items[0]?.task_id;
  const changeGroup = (v: string) => {
    setGroup(v);
    setPage(1);
    setSelected(undefined);
  };
  return (
    <div className="business-page audit-page">
      <PageHeading
        title="审核任务"
        description="逐文件配置审核依据，跟踪执行过程，集中查看审核结果。"
        backTo="/home"
        backLabel="返回工作台"
        actions={
          <Link to="/audit-tasks/new">
            <Button type="primary" icon={<PlusOutlined />}>
              新建审核任务
            </Button>
          </Link>
        }
      />
      <div className="audit-band">
        {[
          ['total', '任务总数', 'all'],
          ['running', '执行中', 'running'],
          ['completed', '已完成', 'completed'],
          ['attention', '任务异常', 'attention'],
        ].map(([k, label, g]) => (
          <button
            type="button"
            key={k}
            aria-pressed={group === g}
            onClick={() => changeGroup(g)}
          >
            {label}
            <strong>
              {data?.overview[k as keyof TaskList['overview']] ?? '—'}
            </strong>
          </button>
        ))}
      </div>
      <div className="audit-toolbar">
        <Segmented
          value={group}
          onChange={(v) => changeGroup(String(v))}
          options={[
            { label: '全部', value: 'all' },
            { label: '执行中', value: 'running' },
            { label: '已完成', value: 'completed' },
            { label: '任务异常', value: 'attention' },
          ]}
        />
        <Input.Search
          placeholder="搜索任务名称或 ID"
          allowClear
          style={{ maxWidth: 320 }}
          onSearch={(v) => {
            setQ(v);
            setPage(1);
            setSelected(undefined);
          }}
        />
      </div>
      {error && <Failure error={error} retry={reload} />}
      <div className="audit-master">
        <Card
          title="任务队列"
          extra={<span className="audit-muted">选择任务查看文件处理状态</span>}
        >
          <Table<Batch>
            rowKey="task_id"
            loading={!data && !error}
            pagination={false}
            dataSource={data?.items || []}
            rowClassName={(r) => (r.task_id === id ? 'is-selected' : '')}
            onRow={(r) => ({ onClick: () => setSelected(r.task_id) })}
            scroll={{ x: 460, y: 480 }}
            locale={{ emptyText: '暂无符合条件的任务' }}
            columns={[
              {
                title: '任务',
                render: (_, r) => (
                  <>
                    <Button
                      type="link"
                      onClick={() => setSelected(r.task_id)}
                      style={{ padding: 0 }}
                    >
                      <EntityLabel id={r.task_id}>{r.display_name}</EntityLabel>
                    </Button>
                    <div className="audit-muted">{dateText(r.created_at)}</div>
                  </>
                ),
              },
              {
                title: '进度',
                width: 130,
                render: (_, r) => (
                  <>
                    <span>
                      {r.finished_files} / {r.total_files} 份已结束
                    </span>
                    <Progress
                      size="small"
                      percent={
                        r.total_files
                          ? Math.round((100 * r.finished_files) / r.total_files)
                          : 0
                      }
                      showInfo={false}
                    />
                  </>
                ),
              },
              {
                title: '状态',
                width: 105,
                render: (_, r) => <Status value={r.status} />,
              },
            ]}
          />
        </Card>
        {id ? (
          <Matrix key={id} id={id} />
        ) : (
          <Card title="文件处理矩阵">
            <Empty description="选择任务后查看处理阶段与文件结果" />
          </Card>
        )}
      </div>
      <div className="audit-pagination">
        <Pagination
          current={page}
          pageSize={10}
          total={data?.total || 0}
          showSizeChanger={false}
          onChange={setPage}
          showTotal={(n) => `共 ${n} 个任务`}
        />
      </div>
    </div>
  );
}
