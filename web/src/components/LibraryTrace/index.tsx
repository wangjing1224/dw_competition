import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  ExclamationCircleOutlined,
  LoadingOutlined,
} from '@ant-design/icons';
import { Card, Tag, Tree } from 'antd';
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
