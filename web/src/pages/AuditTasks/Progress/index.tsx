import { useParams, useSearchParams } from '@umijs/max';
import {
  Alert,
  Button,
  Drawer,
  Empty,
  Grid,
  Segmented,
  Select,
  Spin,
} from 'antd';
import { useEffect, useMemo, useState } from 'react';
import EntityLabel from '@/components/EntityLabel';
import { Failure, PageHeading } from '@/pages/RuleLibraries/shared';
import type {
  ProgressData,
  TaskDetail,
  TraceUpdate,
} from '@/services/auditTasks';
import { api, mergeTrace } from '@/services/auditTasks';
import { fileLabel } from '@/utils/displayLabels';
import { Binding, FileErrors, useRemote } from '../shared';
import AgentGraph from './AgentGraph';
import ExecutionOverview from './ExecutionOverview';
import ExecutionRecords from './ExecutionRecords';
import GraphCanvas from './GraphCanvas';
import { agentGraph, ancestors, flowGraph } from './graphModel';
import type { Selection } from './Inspector';
import Inspector from './Inspector';
import TraceTree from './TraceTree';
import './execution.less';

export type View = 'flow' | 'agent' | 'tree';
export const readView = (value: string | null): View =>
  value === 'agent' || value === 'tree' ? value : 'flow';
function FileWorkspace({
  taskId,
  fileId,
  view,
  spanId,
  onNavigate,
  onProgress,
}: {
  taskId: string;
  fileId: string;
  view: View;
  spanId?: string;
  onNavigate: (view: View, span?: string) => void;
  onProgress: (data: ProgressData) => void;
}) {
  const [progress, setProgress] = useState<ProgressData>();
  const [updates, setUpdates] = useState<TraceUpdate[]>([]);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const [selection, setSelection] = useState<Selection | undefined>(
    spanId ? { type: 'span', id: spanId } : undefined,
  );
  const [session, setSession] = useState<string>();
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [records, setRecords] = useState(false);
  const [focus, setFocus] = useState<{ id: string; nonce: number }>();
  const screens = Grid.useBreakpoint();
  useEffect(() => {
    if (spanId) setSelection({ type: 'span', id: spanId });
  }, [spanId]);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let cursor = 0;
    setUpdates([]);
    setProgress(undefined);
    const tick = async () => {
      try {
        const query = new URLSearchParams({
          include_trace: 'true',
          file_id: fileId,
          after_seq: String(cursor),
          limit: '150',
        });
        const d = await api<ProgressData>(`/${taskId}/progress?${query}`, {
          signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        if (
          d.task.task_id !== taskId ||
          !d.files.some((f) => f.file_id === fileId) ||
          (d.activity.scope && d.activity.scope !== 'file') ||
          (d.activity.file_id && d.activity.file_id !== fileId)
        )
          throw new Error('进度数据归属不符');
        setError('');
        setProgress(d);
        onProgress(d);
        setUpdates((prev) =>
          mergeTrace(
            prev,
            d.trace_updates.filter(
              (u) =>
                u.span.file_id === fileId &&
                (!u.span.task_id || u.span.task_id === taskId),
            ),
          ),
        );
        cursor = d.next_seq;
        // 完成后仍排空增量页；排队中的任务也需要继续轮询。
        if (
          d.has_more ||
          ['queued', 'running'].includes(d.task.status) ||
          d.files.some((f) => ['queued', 'running'].includes(f.status))
        )
          timer = setTimeout(tick, d.has_more ? 50 : 1500);
      } catch (e) {
        if (!controller.signal.aborted) {
          setError((e as Error).message);
          timer = setTimeout(tick, 4000);
        }
      }
    };
    void tick();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [taskId, fileId, retry, onProgress]);
  const nodes = useMemo(() => updates.map((u) => u.span), [updates]);
  const file = progress?.files.find((f) => f.file_id === fileId);
  const flow = useMemo(
    () => (file ? flowGraph(file, nodes) : { nodes: [], edges: [] }),
    [file, nodes],
  );
  const agent = useMemo(
    () => agentGraph(nodes, session, collapsed),
    [nodes, session, collapsed],
  );
  const selectSpan = (id: string) => {
    setRecords(false);
    setSelection({ type: 'span', id });
    onNavigate('tree', id);
  };
  const changeView = (next: View) => {
    // 手动切页签不携带其他视图的详情；显式“在图中定位”由 locate 单独处理。
    setSelection(undefined);
    setFocus(undefined);
    onNavigate(next);
  };
  const close = () => {
    setSelection(undefined);
    setFocus(undefined);
    onNavigate(view);
  };
  const selectFlow = (id: string) => {
    setSelection({ type: 'flow', id });
    setFocus(undefined);
    onNavigate('flow');
  };
  const locate = (id: string) => {
    const span = nodes.find((s) => s.span_id === id);
    if (!span) return;
    const chain = [
      span,
      ...ancestors(span, new Map(nodes.map((s) => [s.span_id, s]))),
    ];
    const root = chain.find(
      (s) => s.kind === 'batch' && s.actor === 'bid_agent',
    );
    if (root) {
      const model = agentGraph(nodes, root.span_id),
        target = model.spanToNode.get(id);
      if (target) {
        setCollapsed(new Set());
        setSession(root.span_id);
        onNavigate('agent', id);
        setFocus({ id: target, nonce: Date.now() });
        return;
      }
    }
    const phase = chain.find((s) =>
      ['file', 'parsing', 'routing', 'indexing', 'auditing', 'export'].includes(
        s.kind,
      ),
    );
    if (phase) {
      const target = phase.kind === 'export' ? 'exporting' : phase.kind;
      onNavigate('flow', id);
      setFocus({ id: target, nonce: Date.now() });
    }
  };
  const node =
    selection?.type === 'flow'
      ? flow.nodes.find((n) => n.id === selection.id)
      : selection?.type === 'agent'
        ? agent.nodes.find((n) => n.id === selection.id)
        : undefined;
  const partial =
    !!progress?.has_more ||
    !!progress?.trace_warning ||
    progress?.activity.observation !== 'complete';
  const routeSpan = nodes.find((n) => n.kind === 'routing');
  const inspector =
    file && selection ? (
      <Inspector
        key={`${fileId}/${selection.type}/${selection.id}`}
        task={taskId}
        file={file}
        selection={selection}
        node={node}
        spans={nodes}
        spanRevision={updates.find((u) => u.span.span_id === selection.id)?.seq}
        onClose={close}
        onSpan={selectSpan}
        onLocate={locate}
        onAgent={() => changeView('agent')}
      />
    ) : null;
  return (
    <>
      {error && (
        <Failure
          error={`连接异常：${error}。保留上次记录，正在重连。`}
          retry={() => setRetry((v) => v + 1)}
        />
      )}
      {!file && !error && <Spin />}
      {file && progress && (
        <>
          {(file.error || file.export_error) && (
            <div className="section-gap">
              <FileErrors file={file} />
            </div>
          )}
          <ExecutionOverview
            task={taskId}
            file={file}
            activity={progress.activity}
            onStage={selectFlow}
            onDraft={() => setSelection({ type: 'draft', id: 'draft' })}
          />
          {progress.trace_warning && (
            <Alert type="warning" title={progress.trace_warning.message} />
          )}
          {progress.has_more && (
            <Alert type="info" title="正在加载剩余轨迹；当前为部分记录" />
          )}
          <section className="audit-panel audit-three-views">
            <header>
              <Segmented
                aria-label="执行视图"
                value={view}
                onChange={changeView}
                options={[
                  { label: '流程图', value: 'flow' },
                  { label: 'Agent 图', value: 'agent' },
                  { label: '轨迹树', value: 'tree' },
                ]}
              />
              <Button onClick={() => setRecords(true)}>执行记录</Button>
            </header>
            <div
              className={
                'audit-visual-body' +
                (inspector && screens.lg ? ' with-inspector' : '')
              }
            >
              <div className="audit-visual-main">
                <div hidden={view !== 'flow'}>
                  <GraphCanvas
                    graph={flow}
                    label="审核流程图"
                    onSelect={(id) =>
                      id === 'auditing' ? changeView('agent') : selectFlow(id)
                    }
                    selected={
                      selection?.type === 'flow' ? selection.id : focus?.id
                    }
                    focus={focus}
                  />
                </div>
                <div hidden={view !== 'agent'}>
                  <AgentGraph
                    file={file}
                    graph={agent}
                    partial={partial}
                    onSession={(id) => {
                      setSession(id);
                      setSelection(undefined);
                      setFocus(undefined);
                      onNavigate('agent');
                    }}
                    selected={
                      selection?.type === 'agent'
                        ? selection.id
                        : selection?.type === 'span'
                          ? agent.spanToNode.get(selection.id)
                          : undefined
                    }
                    onSelect={(id) => {
                      setSelection({ type: 'agent', id });
                      onNavigate('agent');
                    }}
                    onFlow={() => changeView('flow')}
                    onRoute={
                      routeSpan
                        ? () => selectSpan(routeSpan.span_id)
                        : undefined
                    }
                    focus={focus}
                    onToggle={(id) => {
                      setCollapsed((prev) => {
                        const next = new Set(prev);
                        if (next.has(id)) next.delete(id);
                        else next.add(id);
                        return next;
                      });
                      setSelection({ type: 'agent', id });
                      onNavigate('agent');
                    }}
                  />
                </div>
                <div hidden={view !== 'tree'}>
                  <TraceTree
                    spans={nodes}
                    selected={
                      selection?.type === 'span' ? selection.id : undefined
                    }
                    onSelect={selectSpan}
                  />
                </div>
              </div>
              {screens.lg && inspector}
            </div>
          </section>
          {!screens.lg && (
            <Drawer
              open={!!selection && !records}
              onClose={close}
              size={480}
              title="执行详情"
            >
              {inspector}
            </Drawer>
          )}
          <ExecutionRecords
            open={records}
            onClose={() => setRecords(false)}
            spans={nodes}
            partial={partial}
            onSelect={selectSpan}
          />
        </>
      )}
    </>
  );
}
function Workspace({ taskId }: { taskId: string }) {
  const [params, setParams] = useSearchParams();
  // 身份只初始化一次；阶段、文件列表与统计共享同一条 progress 轮询。
  const initial = useRemote(
    (signal) => api<TaskDetail>(`/${taskId}`, { signal }),
    [taskId],
  );
  const [live, setLive] = useState<ProgressData>();
  const data = live || initial.data;
  const file =
    data?.files.find((f) => f.file_id === params.get('file')) || data?.files[0];
  const view = readView(params.get('view'));
  const navigate = (next: View, span?: string) => {
    const p = new URLSearchParams();
    if (file) p.set('file', file.file_id);
    p.set('view', next);
    if (span) p.set('span', span);
    setParams(p, { replace: true });
  };
  return (
    <div className="business-page audit-page">
      <PageHeading
        title="文件执行工作区"
        description={
          data ? (
            <EntityLabel id={taskId}>
              任务：{data.task.display_name}
            </EntityLabel>
          ) : undefined
        }
        backTo="/audit-tasks"
        backLabel="返回审核任务"
      />
      {initial.error && (
        <Failure error={initial.error} retry={initial.reload} />
      )}
      {!data && !initial.error && <Spin />}
      {data && (
        <>
          <div className="audit-toolbar audit-context-toolbar">
            <Select
              aria-label="当前投标文件"
              className="audit-file-selector"
              value={file?.file_id}
              onChange={(id) => setParams({ file: id, view })}
              options={data.files.map((f) => ({
                value: f.file_id,
                label: fileLabel(f.file_id, data.files),
              }))}
            />
            {file && (
              <div className="audit-context-bindings">
                <Binding file={file} />
              </div>
            )}
            <span className="audit-muted audit-context-count">
              {data.task.finished_files} / {data.task.total_files} 份文件已结束
            </span>
          </div>
          {file ? (
            <FileWorkspace
              key={`${taskId}/${file.file_id}`}
              taskId={taskId}
              fileId={file.file_id}
              view={view}
              spanId={params.get('span') || undefined}
              onNavigate={navigate}
              onProgress={setLive}
            />
          ) : (
            <Empty description="暂无文件记录" />
          )}
        </>
      )}
    </div>
  );
}
export default function AuditProgress() {
  const { taskId = '' } = useParams();
  return <Workspace key={taskId} taskId={taskId} />;
}
