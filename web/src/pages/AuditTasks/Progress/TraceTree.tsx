import { Alert, Button, Empty, Space, Tree } from 'antd';
import type { DataNode } from 'antd/es/tree';
import type { ComponentRef, Key } from 'react';
import { useEffect, useMemo, useRef, useState } from 'react';
import type { Span } from '@/services/auditTasks';
import { Status } from '../shared';
import { ancestors, duration, spanTitle, traceForest } from './graphModel';

export default function TraceTree({
  spans,
  selected,
  onSelect,
}: {
  spans: Span[];
  selected?: string;
  onSelect: (id: string) => void;
}) {
  const forest = useMemo(() => traceForest(spans), [spans]);
  const [expanded, setExpanded] = useState<Key[]>([]);
  const seen = useRef(new Set<string>());
  const ref = useRef<ComponentRef<typeof Tree>>(null);
  const viewport = useRef<HTMLDivElement>(null);
  const [height, setHeight] = useState(480);
  useEffect(() => {
    const el = viewport.current;
    if (!el) return;
    const measure = () => {
      if (el.clientHeight) setHeight(el.clientHeight);
    };
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    measure();
    return () => observer.disconnect();
  }, []);
  const selectedNode = selected ? forest.byId.get(selected) : undefined;
  const selectedPath = selectedNode
    ? JSON.stringify(ancestors(selectedNode, forest.byId).map((s) => s.span_id))
    : undefined;
  useEffect(() => {
    const fresh = spans.filter((s) => !seen.current.has(s.span_id));
    for (const s of spans) seen.current.add(s.span_id);
    if (fresh.length)
      setExpanded((keys) => [
        ...new Set([...keys, ...fresh.map((s) => s.span_id)]),
      ]);
  }, [spans]);
  useEffect(() => {
    if (selected && selectedPath !== undefined) {
      setExpanded((keys) => [
        ...new Set([...keys, ...(JSON.parse(selectedPath) as string[])]),
      ]);
      const frame = requestAnimationFrame(() =>
        ref.current?.scrollTo({ key: selected }),
      );
      return () => cancelAnimationFrame(frame);
    }
    return undefined;
    // 状态轮询不能反复展开/滚动；仅显式选中或父链补齐时定位。
  }, [selected, selectedPath]);
  const title = (s: Span) => (
    <Space size={6}>
      <span>{spanTitle(s, spans)}</span>
      <Status value={s.status} />
      <small className="audit-muted">{duration(s)}</small>
    </Space>
  );
  const children = (id: string): DataNode[] =>
    (forest.children.get(id) || []).map((s) => ({
      key: s.span_id,
      title: title(s),
      children: children(s.span_id),
    }));
  return (
    <div className="audit-trace-tree">
      <div className="audit-tree-toolbar">
        <span className="audit-muted">逐次调用 · 默认展开</span>
        <Space size={4}>
          <Button
            size="small"
            onClick={() => setExpanded(spans.map((s) => s.span_id))}
          >
            全部展开
          </Button>
          <Button size="small" onClick={() => setExpanded([])}>
            全部收起
          </Button>
        </Space>
      </div>
      <div className="audit-tree-viewport" ref={viewport}>
        {!spans.length ? (
          <Empty description="尚无已记录调用" />
        ) : (
          <Tree
            ref={ref}
            blockNode
            showLine
            height={height}
            treeData={children('')}
            expandedKeys={expanded}
            onExpand={setExpanded}
            selectedKeys={selected ? [selected] : []}
            onSelect={(keys) => keys[0] && onSelect(String(keys[0]))}
          />
        )}
        {!!forest.detached.length && (
          <>
            <Alert
              type="warning"
              title="关联信息不完整：以下记录的父链尚未取得，未强行接入其他会话"
            />
            {forest.detached.map((s) => (
              <Button
                key={s.span_id}
                type="text"
                onClick={() => onSelect(s.span_id)}
              >
                {title(s)}
              </Button>
            ))}
          </>
        )}
      </div>
    </div>
  );
}
