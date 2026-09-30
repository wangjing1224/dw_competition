import { Link, useParams, useSearchParams } from '@umijs/max';
import EntityLabel from '@/components/EntityLabel';
import { fileLabel } from '@/utils/displayLabels';
import {
  Alert,
  Button,
  Empty,
  Segmented,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Tree,
} from 'antd';
import { useEffect, useMemo, useState } from 'react';
import type { DataNode } from 'antd/es/tree';
import { PageHeading, Failure } from '@/pages/RuleLibraries/shared';
import { api, fileUrl, mergeTrace } from '@/services/auditTasks';
import type {
  ProgressData,
  ReportData,
  Span,
  SpanDetail,
  TaskDetail,
  TraceUpdate,
} from '@/services/auditTasks';
import { useRemote, Status, Binding, Markdown, FileErrors } from '../shared';

const actors: Record<string, string> = {
  system: '系统',
  router: '适用性判断',
  bid_agent: '主审核 Agent',
  evidence_subagent: '资料核验 Agent',
};
function Inspector({
  task,
  file,
  spanId,
  revision,
  kind,
  spanRevision,
}: {
  task: string;
  file: string;
  spanId?: string;
  revision: string | null;
  kind: string;
  spanRevision?: number;
}) {
  const [tab, setTab] = useState('节点详情');
  const node = useRemote(
    (s) =>
      spanId
        ? api<SpanDetail>(`/${task}/trace/${spanId}`, { signal: s })
        : Promise.resolve(null),
    [task, spanId, spanRevision],
  );
  const draft = useRemote(
    (s) =>
      tab === '报告草稿'
        ? api<ReportData>(`/${task}/files/${file}`, { signal: s })
        : Promise.resolve(null),
    [task, file, revision, kind, tab],
  );
  return (
    <section className="audit-panel">
      <header>
        <Segmented
          value={tab}
          onChange={setTab}
          options={['节点详情', '报告草稿']}
        />
      </header>
      <div className="audit-inspector">
        {tab === '报告草稿' ? (
          <>
            {draft.error && (
              <Failure error={draft.error} retry={draft.reload} />
            )}
            <Tag color={kind === 'final' ? 'green' : 'orange'}>
              {kind === 'final' ? '已定稿' : '未定稿 · 最近一次保存'}
            </Tag>
            {draft.data?.document.markdown ? (
              <Markdown text={draft.data.document.markdown} />
            ) : (
              <Empty description="尚无已保存正文" />
            )}
          </>
        ) : (
          <>
            {node.error && <Failure error={node.error} retry={node.reload} />}{' '}
            {!spanId ? (
              <Empty description="选择一个调用节点查看安全参数与可见结果" />
            ) : !node.data ? (
              <Spin />
            ) : (
              <>
                <h3>{node.data.span.title}</h3>
                <Space wrap>
                  <Status value={node.data.span.status} />
                  <Tag>
                    {actors[node.data.span.actor] || node.data.span.actor}
                  </Tag>
                  <span>
                    {node.data.span.duration_ms === null
                      ? '执行中'
                      : `${(node.data.span.duration_ms / 1000).toFixed(2)}s`}
                  </span>
                </Space>
                {node.data.span.rule_id && (
                  <p>核验规则：{node.data.span.rule_id}</p>
                )}
                {!!node.data.span.target_file_ids?.length && (
                  <p>授权资料：{node.data.span.target_file_ids.join('、')}</p>
                )}
                {node.data.truncated && (
                  <Alert
                    type="info"
                    title="展示内容已脱敏或截断，不影响业务结果"
                  />
                )}
                {node.data.error && (
                  <Alert type="error" title={node.data.error.message} />
                )}
                <h4>调用输入</h4>
                <pre>{JSON.stringify(node.data.input, null, 2) || '无'}</pre>
                <h4>可见输出</h4>
                <pre>
                  {typeof node.data.output === 'string'
                    ? node.data.output
                    : JSON.stringify(node.data.output, null, 2) || '尚无返回'}
                </pre>
              </>
            )}
          </>
        )}
      </div>
    </section>
  );
}
function Graph({
  nodes,
  onSelect,
  selected,
  scale,
  tree,
}: {
  nodes: Span[];
  onSelect: (id: string) => void;
  selected?: string;
  scale: number;
  tree: boolean;
}) {
  const [closed, setClosed] = useState<Set<string>>(new Set());
  const byParent = useMemo(() => {
    const known = new Set(nodes.map((n) => n.span_id));
    const map = new Map<string, Span[]>();
    nodes.forEach((n) => {
      // 父节点还没加载时暂存，不伪造为根节点或另一个会话的分支。
      if (n.parent_span_id && !known.has(n.parent_span_id)) return;
      const p = n.parent_span_id || '';
      map.set(p, [...(map.get(p) || []), n]);
    });
    return map;
  }, [nodes]);
  const treeNodes = (id: string): DataNode[] =>
    (byParent.get(id) || []).map((n) => ({
      key: n.span_id,
      title: (
        <Space>
          <span>{n.title}</span>
          <Status value={n.status} />
        </Space>
      ),
      children: treeNodes(n.span_id),
    }));
  const draw = (id: string): React.ReactNode => (
    <ul>
      {(byParent.get(id) || []).map((n) => (
        <li key={n.span_id}>
          <Space>
            <button
              type="button"
              className={`audit-node ${selected === n.span_id ? 'active' : ''} ${n.actor === 'evidence_subagent' ? 'child-agent' : ''}`}
              onClick={() => onSelect(n.span_id)}
            >
              <span className="node-actor">
                {actors[n.actor] || n.actor} · {n.kind}
              </span>
              {n.title} <Status value={n.status} />
              <small>
                {n.duration_ms === null
                  ? '运行中'
                  : `${(n.duration_ms / 1000).toFixed(2)}s`}
              </small>
            </button>
            {byParent.has(n.span_id) && (
              <Button
                size="small"
                type="text"
                aria-label={closed.has(n.span_id) ? '展开分支' : '折叠分支'}
                onClick={() =>
                  setClosed((s) => {
                    const t = new Set(s);
                    if (t.has(n.span_id)) t.delete(n.span_id);
                    else t.add(n.span_id);
                    return t;
                  })
                }
              >
                {closed.has(n.span_id) ? '+' : '−'}
              </Button>
            )}
          </Space>
          {!closed.has(n.span_id) && draw(n.span_id)}
        </li>
      ))}
    </ul>
  );
  if (!nodes.length)
    return <Empty description="尚无调用记录；接收与等待不代表模型已执行" />;
  return tree ? (
    <Tree
      treeData={treeNodes('')}
      defaultExpandAll
      selectedKeys={selected ? [selected] : []}
      onSelect={(keys) => keys[0] && onSelect(String(keys[0]))}
    />
  ) : (
    <div className="audit-graph" style={{ zoom: scale }}>
      {draw('')}
    </div>
  );
}
function FileWorkspace({ taskId, fileId }: { taskId: string; fileId: string }) {
  const [progress, setProgress] = useState<ProgressData>();
  const [updates, setUpdates] = useState<TraceUpdate[]>([]);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const [selected, setSelected] = useState<string>();
  const [view, setView] = useState('调用图');
  const [scale, setScale] = useState(1);
  const [eventFilter, setEventFilter] = useState('all');
  useEffect(() => {
    const c = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let cursor = 0;
    setUpdates([]);
    setProgress(undefined);
    setSelected(undefined);
    const tick = async () => {
      try {
        const d = await api<ProgressData>(
          `/${taskId}/progress?${new URLSearchParams({ include_trace: 'true', file_id: fileId, after_seq: String(cursor), limit: '150' })}`,
          { signal: c.signal },
        );
        if (c.signal.aborted) return;
        setError('');
        setProgress(d);
        setUpdates((p) => mergeTrace(p, d.trace_updates));
        cursor = d.next_seq;
        if (d.has_more || d.task.status === 'running')
          timer = setTimeout(tick, d.has_more ? 50 : 1500);
      } catch (e) {
        if (!c.signal.aborted) {
          setError((e as Error).message);
          timer = setTimeout(tick, 4000);
        }
      }
    };
    void tick();
    return () => {
      c.abort();
      clearTimeout(timer);
    };
  }, [taskId, fileId, retry]);
  const nodes = updates.map((u) => u.span);
  const file = progress?.files.find((f) => f.file_id === fileId);
  const events = [...nodes]
    .reverse()
    .filter(
      (n) =>
        eventFilter === 'all' ||
        (eventFilter === 'errors' &&
          ['failed', 'interrupted'].includes(n.status)) ||
        (eventFilter === 'tools' && n.kind === 'tool') ||
        n.actor === eventFilter,
    );
  return (
    <>
      {error && (
        <Failure
          error={`连接异常：${error}。保留上次记录，正在重连。`}
          retry={() => setRetry((v) => v + 1)}
        />
      )}{' '}
      {file && (
        <>
          <Binding file={file} />
          <div className="section-gap">
            <FileErrors file={file} />
          </div>
          <div className="audit-band">
            {[
              ['model_calls', '模型调用'],
              ['tool_calls', '工具调用'],
              ['evidence_checks', '资料核验'],
              ['report_saves', '报告保存'],
            ].map(([key, label]) => (
              <div key={key}>
                {label}
                <strong>
                  {progress?.activity[key as 'model_calls'] ?? '—'}
                </strong>
              </div>
            ))}
          </div>
          <div className="audit-muted">
            当前文件已观测调用次数，不代表完成百分比；不展示隐藏推理。
          </div>
        </>
      )}
      {progress?.trace_warning && (
        <Alert
          className="section-gap"
          type="warning"
          title={progress.trace_warning.message}
        />
      )}
      {progress?.has_more && (
        <Alert type="info" title="正在加载剩余轨迹；当前为部分记录" />
      )}
      <div className="audit-workspace section-gap">
        <section className="audit-panel">
          <header>
            <Segmented
              options={['调用图', '轨迹树']}
              value={view}
              onChange={setView}
            />
            <Space>
              <Button
                size="small"
                onClick={() => setScale((s) => Math.max(0.5, s - 0.1))}
              >
                缩小
              </Button>
              <Button size="small" onClick={() => setScale(1)}>
                重置
              </Button>
              <Button
                size="small"
                onClick={() => setScale((s) => Math.min(1.5, s + 0.1))}
              >
                放大
              </Button>
            </Space>
          </header>
          <div className="audit-canvas">
            <Graph
              nodes={nodes}
              selected={selected}
              onSelect={setSelected}
              scale={scale}
              tree={view === '轨迹树'}
            />
          </div>
        </section>
        <Inspector
          task={taskId}
          file={fileId}
          spanId={selected}
          spanRevision={updates.find((u) => u.span.span_id === selected)?.seq}
          revision={file?.report_revision || null}
          kind={file?.report_kind || 'none'}
        />
      </div>
      <section className="audit-panel audit-events">
        <header>
          <strong>事件流水 · {nodes.length} 个已观测节点</strong>
          <Select
            value={eventFilter}
            onChange={setEventFilter}
            style={{ width: 160 }}
            options={[
              { value: 'all', label: '全部节点' },
              { value: 'bid_agent', label: '主 Agent' },
              { value: 'evidence_subagent', label: '资料子 Agent' },
              { value: 'tools', label: '工具调用' },
              { value: 'errors', label: '异常节点' },
            ]}
          />
        </header>
        <Table<Span>
          size="small"
          rowKey="span_id"
          dataSource={events}
          rowClassName={(r) =>
            r.span_id === selected ? 'audit-selected-row' : ''
          }
          onRow={(r) => ({ onClick: () => setSelected(r.span_id) })}
          scroll={{ x: 650, y: 240 }}
          pagination={{ pageSize: 8, showSizeChanger: false }}
          columns={[
            {
              title: '时间',
              width: 105,
              render: (_, r) => new Date(r.started_at).toLocaleTimeString(),
            },
            { title: '执行方', render: (_, r) => actors[r.actor] || r.actor },
            { title: '动作', dataIndex: 'title' },
            { title: '状态', render: (_, r) => <Status value={r.status} /> },
            {
              title: '耗时',
              render: (_, r) =>
                r.duration_ms === null
                  ? '—'
                  : `${(r.duration_ms / 1000).toFixed(2)}s`,
            },
          ]}
        />
      </section>
    </>
  );
}
export default function AuditProgress() {
  const { taskId = '' } = useParams();
  const [params, setParams] = useSearchParams();
  const { data, error, reload } = useRemote(
    (s) => api<TaskDetail>(`/${taskId}`, { signal: s }),
    [taskId],
    (d) => d.task.status === 'running',
  );
  const file =
    data?.files.find((f) => f.file_id === params.get('file')) || data?.files[0];
  return (
    <div className="business-page audit-page">
      <PageHeading
        title={
          <EntityLabel id={taskId}>
            {data?.task.display_name || '审核执行工作区'}
          </EntityLabel>
        }
        description="查看真实调用与逐文件进展，已保存成果可随时查看。"
        backTo="/audit-tasks"
        backLabel="返回审核任务"
        actions={
          <Space>
            {data && <Status value={data.task.status} />}
            {file && (
              <Link to={fileUrl(taskId, file.file_id)}>
                <Button type="primary" disabled={file.report_kind === 'none'}>
                  查看当前报告
                </Button>
              </Link>
            )}
          </Space>
        }
      />
      {error && <Failure error={error} retry={reload} />}{' '}
      {!data && !error && <Spin />}
      {data && (
        <>
          <div className="audit-toolbar">
            <Select
              aria-label="当前投标文件"
              className="audit-file-selector"
              value={file?.file_id}
              onChange={(v) => setParams({ file: v })}
              options={data.files.map((f) => ({
                value: f.file_id,
                label: fileLabel(f.file_id, data.files),
              }))}
            />
            <span>
              {data.task.finished_files} / {data.task.total_files} 份文件已结束
            </span>
          </div>
          {file ? (
            <FileWorkspace
              key={`${taskId}/${file.file_id}`}
              taskId={taskId}
              fileId={file.file_id}
            />
          ) : (
            <Empty
              description={
                data.task.legacy
                  ? '历史任务未记录逐文件执行轨迹'
                  : '暂无文件记录'
              }
            />
          )}
        </>
      )}
    </div>
  );
}
