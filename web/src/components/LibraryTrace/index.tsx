import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  ExclamationCircleOutlined,
  LoadingOutlined,
} from '@ant-design/icons';
import { Alert, Card, Space, Tag, Tree } from 'antd';
import type { DataNode } from 'antd/es/tree';
import type { ReactNode } from 'react';
import type { TraceNode } from '@/services/ruleLibraries';
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

// 两个库的接口保持独立，仅统一展示：规则输出包在 content 中，资料输出也可能直接是字符串。
export function TraceDetail({
  detail,
}: {
  detail: {
    node: Pick<TraceNode, 'status' | 'tool_call_id'>;
    input: unknown;
    output: unknown;
    error: { message: string } | null;
    truncated: boolean;
    material_change?: unknown;
  };
}) {
  const output =
    detail.output &&
    typeof detail.output === 'object' &&
    !Array.isArray(detail.output)
      ? (detail.output as Record<string, unknown>)
      : undefined;
  const images = Array.isArray(output?.images)
    ? output.images.filter(
        (image): image is { url: string; path: string } =>
          image !== null &&
          typeof image === 'object' &&
          typeof image.url === 'string' &&
          typeof image.path === 'string',
      )
    : [];
  const content =
    typeof detail.output === 'string'
      ? detail.output
      : typeof output?.content === 'string'
        ? output.content
        : undefined;
  return (
    <>
      <Space wrap>
        <Tag>{statusLabels[detail.node.status] || detail.node.status}</Tag>
        {detail.node.tool_call_id && <code>{detail.node.tool_call_id}</code>}
      </Space>
      {detail.truncated && (
        <Alert type="warning" title="长内容仅展示前部，实际调用内容未改变" />
      )}
      {detail.error && <Alert type="error" title={detail.error.message} />}
      <h3>输入</h3>
      <pre className="trace-content">
        {detail.input == null ? '无' : JSON.stringify(detail.input, null, 2)}
      </pre>
      <h3>输出</h3>
      {(content !== undefined || images.length === 0) && (
        <pre className="trace-content">
          {content ??
            (detail.output == null
              ? '等待返回'
              : JSON.stringify(detail.output, null, 2))}
        </pre>
      )}
      {images.map((image) => (
        <img
          key={image.path}
          alt={image.path}
          src={image.url}
          style={{ maxWidth: '100%' }}
        />
      ))}
      {output?.rule_changes != null && (
        <>
          <h3>已提交规则变更</h3>
          <pre className="trace-content">
            {JSON.stringify(output.rule_changes, null, 2)}
          </pre>
        </>
      )}
      {detail.material_change != null && (
        <>
          <h3>已保存变更（修改前 → 修改后）</h3>
          <pre className="trace-content">
            {JSON.stringify(detail.material_change, null, 2)}
          </pre>
        </>
      )}
    </>
  );
}

export function TracePanel({
  title,
  label,
  extra,
  children,
}: {
  title: ReactNode;
  label: string;
  extra?: ReactNode;
  children: ReactNode;
}) {
  return (
    <Card className="trace-panel" title={title} extra={extra}>
      {/* biome-ignore lint/a11y/noNoninteractiveTabindex: 独立滚动区域需要支持键盘聚焦与方向键滚动。 */}
      <section className="trace-panel-scroll" aria-label={label} tabIndex={0}>
        {children}
      </section>
    </Card>
  );
}

export function TraceTree({
  nodes,
  expandedKeys,
  onExpand,
  onSelect,
}: {
  nodes: Record<string, SeenNode>;
  expandedKeys?: string[];
  onExpand: (keys: string[]) => void;
  onSelect: (key?: string) => void;
}) {
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
  return (
    <Tree
      className="library-trace-tree"
      showIcon
      blockNode
      expandAction="click"
      autoExpandParent={false}
      expandedKeys={
        expandedKeys ??
        Object.values(nodes)
          .filter(({ node }) => ['file', 'batch', 'turn'].includes(node.kind))
          .map(({ node }) => node.span_id)
      }
      onExpand={(keys) => onExpand(keys.map(String))}
      treeData={children(null)}
      onSelect={(keys) => onSelect(keys[0] ? String(keys[0]) : undefined)}
    />
  );
}
