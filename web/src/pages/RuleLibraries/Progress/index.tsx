import { useEffect, useRef, useState } from 'react';
import { Link, useParams } from '@umijs/max';
import {
  Alert,
  Button,
  Card,
  Drawer,
  Empty,
  Space,
  Steps,
  Tag,
  Tree,
} from 'antd';
import type { DataNode } from 'antd/es/tree';
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  LoadingOutlined,
  NodeIndexOutlined,
  ExclamationCircleOutlined,
} from '@ant-design/icons';
import {
  api,
  phases,
  type ProgressData,
  type NodeDetail,
  type TraceNode,
  type Detail,
} from '@/services/ruleLibraries';
import { Failure, PageHeading, RuleCard, Status } from '../shared';

export type SeenNode = { seq: number; node: TraceNode };
export function mergeChanges(
  previous: Record<string, SeenNode>,
  changes: SeenNode[],
) {
  const next = { ...previous };
  changes.forEach((change) => {
    if (
      !next[change.node.span_id] ||
      next[change.node.span_id].seq < change.seq
    )
      next[change.node.span_id] = change;
  });
  return next;
}
const statusLabels: Record<string, string> = {
  running: '执行中',
  succeeded: '已完成',
  partial: '部分完成',
  failed: '失败',
  interrupted: '中断',
};
export default function ProgressPage() {
  const { projectId = '' } = useParams();
  const [data, setData] = useState<ProgressData>();
  const [error, setError] = useState('');
  const [nodes, setNodes] = useState<Record<string, SeenNode>>({});
  const [expandedKeys, setExpandedKeys] = useState<string[]>();
  const [selected, setSelected] = useState<string>();
  const [detail, setDetail] = useState<NodeDetail>();
  const [detailError, setDetailError] = useState('');
  const [reload, setReload] = useState(0);
  const [final, setFinal] = useState<Detail>();
  const cursor = useRef(0);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    cursor.current = 0;
    setNodes({});
    setExpandedKeys(undefined);
    setSelected(undefined);
    setFinal(undefined);
    setData(undefined);
    async function poll() {
      let delay = 2000;
      try {
        if (!document.hidden) {
          const result = await api<ProgressData>(
            `/${projectId}/progress?include_trace=true&after_seq=${cursor.current}&limit=100`,
            { signal: controller.signal },
          );
          setData(result);
          setError('');
          setNodes((old) => mergeChanges(old, result.trace?.changes || []));
          cursor.current = result.trace?.next_cursor ?? cursor.current;
          if (result.trace?.has_more) delay = 0;
          else if (
            result.status !== 'processing_rules' &&
            result.trace?.trace_status !== 'recording'
          ) {
            setFinal(
              await api<Detail>(`/${projectId}`, { signal: controller.signal }),
            );
            return;
          }
        }
      } catch (e) {
        if ((e as Error).name !== 'AbortError') setError((e as Error).message);
        delay = 6000;
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, delay);
    }
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [projectId, reload]);
  const selectedSeq = selected ? nodes[selected]?.seq : undefined;
  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    setDetailError('');
    setDetail(undefined);
    api<NodeDetail>(`/${projectId}/trace/${selected}`, {
      signal: controller.signal,
    })
      .then(setDetail)
      .catch((e) => {
        if (e.name !== 'AbortError') setDetailError(e.message);
      });
    return () => controller.abort();
  }, [projectId, selected, selectedSeq]);
  const children = (parent: string | null): DataNode[] =>
    Object.values(nodes)
      .filter((n) => n.node.parent_span_id === parent)
      .sort(
        (a, b) =>
          a.node.started_at.localeCompare(b.node.started_at) || a.seq - b.seq,
      )
      .map(({ node }) => ({
        key: node.span_id,
        selectable: node.has_detail,
        title: (
          <span className="trace-title">
            <span>{node.title}</span>
            <Tag
              color={
                node.status === 'failed'
                  ? 'red'
                  : node.status === 'running'
                    ? 'blue'
                    : ['partial', 'interrupted'].includes(node.status)
                      ? 'orange'
                      : 'default'
              }
            >
              {statusLabels[node.status]}
            </Tag>
            {node.duration_ms !== null && (
              <small>{(node.duration_ms / 1000).toFixed(2)}s</small>
            )}
          </span>
        ),
        icon:
          node.status === 'running' ? (
            <LoadingOutlined />
          ) : node.status === 'failed' ? (
            <CloseCircleOutlined />
          ) : ['partial', 'interrupted'].includes(node.status) ? (
            <ExclamationCircleOutlined />
          ) : (
            <CheckCircleOutlined />
          ),
        children: children(node.span_id),
      }));
  const phaseOrder = [
    'received',
    'parsing',
    'indexing',
    'extracting',
    'saving',
    'completed',
  ];
  return (
    <main className="business-page">
      <PageHeading
        title={data?.display_name || '规则抽取进度'}
        description="追踪真实文件处理、模型调用与工具执行；已保存成果可随时查看。"
        actions={
          <>
            <Status status={data?.status || 'processing_rules'} />
            <Link to={`/rule-libraries/${projectId}`}>
              <Button>查看当前规则</Button>
            </Link>
          </>
        }
      />
      {error && <Failure error={error} retry={() => setReload((n) => n + 1)} />}
      {data?.error && (
        <Alert
          className="section-gap"
          type="error"
          showIcon
          title={data.error.message}
          description={`失败阶段：${phases[data.failed_phase || ''] || '未知'}。已保存的规则和原文件保留，不自动重跑。`}
        />
      )}
      <Card className="section-gap">
        <Steps
          size="small"
          current={Math.max(
            0,
            phaseOrder.indexOf(
              data?.phase === 'failed'
                ? data.failed_phase || ''
                : data?.phase || 'received',
            ),
          )}
          status={data?.status === 'rule_failed' ? 'error' : undefined}
          items={phaseOrder.map((key) => ({ title: phases[key] }))}
        />
        <div className="progress-summary">
          <span>
            已完成文件{' '}
            <strong>
              {data?.completed_file_ids.length || 0} /{' '}
              {data?.document_count || 0}
            </strong>
          </span>
          <span>
            已保存规则 <strong>{data?.saved_rule_count || 0}</strong>
          </span>
          <span>
            {data?.status === 'processing_rules'
              ? '后台处理中，可离开页面后再返回'
              : '处理已结束'}
          </span>
        </div>
      </Card>
      {final && (
        <Alert
          className="section-gap"
          type={
            data?.status === 'rule_failed' || final.warning
              ? 'warning'
              : 'success'
          }
          title={
            data?.status === 'rule_failed'
              ? '处理失败，可查看已保存的阶段结果和来源文件'
              : final.warning?.code === 'rule_set_unavailable'
                ? '规则快照不可读取，仍可查看来源文件'
                : final.rule_set?.rules.length === 0
                  ? '处理完成，但未提取到规则'
                  : '处理结果已保存'
          }
          action={
            <Link to={`/rule-libraries/${projectId}`}>
              <Button type="primary">进入规则审阅</Button>
            </Link>
          }
        />
      )}
      <div className="trace-grid">
        <Card title={`来源文件 · ${data?.document_count || 0}`}>
          <div className="file-progress-list">
            {data?.files.map((file) => (
              <div
                key={file.file_id}
                className={`file-progress ${file.extraction_status === 'processing' ? 'active' : ''}`}
              >
                <strong>{file.file_name}</strong>
                <small>{file.source_path}</small>
                <Space wrap>
                  <code>{file.file_id}</code>
                  <Tag
                    color={
                      file.extraction_status === 'completed'
                        ? 'green'
                        : file.extraction_status === 'failed'
                          ? 'red'
                          : 'blue'
                    }
                  >
                    {
                      {
                        pending: '等待抽取',
                        processing: '抽取中',
                        completed: '已完成',
                        failed: '失败',
                      }[file.extraction_status || 'pending']
                    }
                  </Tag>
                </Space>
                <small>
                  解析：
                  {(
                    {
                      pending: '等待解析',
                      parsed: '完成',
                      partial: '部分可读',
                      ocr_required: '需识图',
                      manual_review: '需人工处理',
                      unsupported: '不支持',
                    } as Record<string, string>
                  )[file.parse_status] || file.parse_status}
                </small>
              </div>
            ))}
          </div>
        </Card>
        <Card
          title={
            <Space>
              <NodeIndexOutlined />
              Agent 执行轨迹
            </Space>
          }
          extra={<span className="muted">点击节点查看详情</span>}
        >
          {data?.trace?.warning && (
            <Alert type="warning" title={data.trace.warning} />
          )}
          {Object.keys(nodes).length ? (
            <Tree
              showIcon
              blockNode
              expandAction="click"
              autoExpandParent={false}
              expandedKeys={
                expandedKeys ??
                Object.values(nodes)
                  .filter(({ node }) =>
                    ['file', 'batch', 'turn'].includes(node.kind),
                  )
                  .map(({ node }) => node.span_id)
              }
              onExpand={(keys) => setExpandedKeys(keys.map(String))}
              treeData={children(null)}
              onSelect={(keys) =>
                setSelected(keys[0] ? String(keys[0]) : undefined)
              }
            />
          ) : (
            <Empty description="尚无 Agent 调用记录；解析和建索引完成后显示。" />
          )}
        </Card>
        <Card title="最近保存的规则">
          <div className="recent-rules">
            {data?.recent_rules.length ? (
              data.recent_rules.map((rule) => (
                <RuleCard key={rule.rule_id} rule={rule} />
              ))
            ) : (
              <Empty description="暂无已保存规则" />
            )}
          </div>
        </Card>
      </div>
      <Drawer
        title={detail?.node.title || '执行详情'}
        open={!!selected}
        onClose={() => setSelected(undefined)}
        size="large"
      >
        {detailError && <Failure error={detailError} />}
        {!detail && !detailError && <p>读取中…</p>}
        {detail && (
          <>
            <Space wrap>
              <Tag>{statusLabels[detail.node.status]}</Tag>
              <code>{detail.node.tool_call_id}</code>
            </Space>
            {detail.truncated && (
              <Alert
                type="warning"
                title="长内容仅展示前部，实际调用内容未改变"
              />
            )}
            {detail.error && (
              <Alert type="error" title={detail.error.message} />
            )}
            <h3>输入</h3>
            <pre className="trace-content">
              {JSON.stringify(detail.input, null, 2) || '无'}
            </pre>
            <h3>输出</h3>
            {detail.output?.images && Array.isArray(detail.output.images) ? (
              (detail.output.images as { url: string; path: string }[]).map(
                (image) => (
                  <img
                    key={image.path}
                    alt={image.path}
                    src={image.url}
                    style={{ maxWidth: '100%' }}
                  />
                ),
              )
            ) : (
              <pre className="trace-content">
                {typeof detail.output?.content === 'string'
                  ? detail.output.content
                  : JSON.stringify(detail.output, null, 2) || '等待返回'}
              </pre>
            )}
            {detail.output?.rule_changes && (
              <>
                <h3>已提交规则变更</h3>
                <pre className="trace-content">
                  {JSON.stringify(detail.output.rule_changes, null, 2)}
                </pre>
              </>
            )}
          </>
        )}
      </Drawer>
    </main>
  );
}
