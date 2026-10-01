// biome-ignore-all lint/a11y/noNoninteractiveTabindex: 画布通过方向键平移，必须可聚焦；内部节点仍使用原生按钮。
import { Button, Space } from 'antd';
import type { ZoomBehavior, ZoomTransform } from 'd3';
import { select, zoom, zoomIdentity } from 'd3';
import type { ReactNode } from 'react';
import { useEffect, useId, useRef, useState } from 'react';
import type { Graph } from './graphModel';
import { statusText } from './graphModel';

export default function GraphCanvas({
  graph,
  selected,
  onSelect,
  label,
  extra,
  focus,
}: {
  graph: Graph;
  selected?: string;
  onSelect: (id: string) => void;
  label: string;
  extra?: ReactNode;
  focus?: { id: string; nonce: number };
}) {
  const host = useRef<HTMLElement>(null);
  const behavior = useRef<ZoomBehavior<HTMLElement, unknown>>(undefined);
  const bounds = useRef({ width: 1, height: 1 });
  const viewport = useRef({ width: 0, height: 0 });
  const [transform, setTransform] = useState<ZoomTransform>(zoomIdentity);
  const marker = useId().replace(/:/g, '');
  bounds.current = {
    width: Math.max(1, ...graph.nodes.map((n) => n.x + n.width)) + 28,
    height: Math.max(1, ...graph.nodes.map((n) => n.y + n.height)) + 28,
  };
  const fitCanvas = () => {
    const el = host.current,
      z = behavior.current;
    if (!el || !z || !el.clientWidth || !el.clientHeight) return;
    const { width, height } = bounds.current;
    const k = Math.min(
      1.1,
      Math.max(
        0.1,
        Math.min(
          (el.clientWidth - 40) / width,
          (el.clientHeight - 40) / height,
        ),
      ),
    );
    viewport.current = { width: el.clientWidth, height: el.clientHeight };
    select(el).call(
      z.transform,
      zoomIdentity
        .translate(
          Math.max(20, (el.clientWidth - width * k) / 2),
          Math.max(20, (el.clientHeight - height * k) / 2),
        )
        .scale(k),
    );
  };
  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const z = zoom<HTMLElement, unknown>()
      .scaleExtent([0.1, 1.8])
      .extent((): [[number, number], [number, number]] => [
        [0, 0],
        [el.clientWidth, el.clientHeight],
      ])
      .filter(
        (event) =>
          !event.button &&
          (event.type === 'wheel' ||
            !(event.target as Element).closest('button')),
      )
      .on('zoom', (event) => setTransform(event.transform));
    behavior.current = z;
    select(el).call(z).on('dblclick.zoom', null);
    const resize = () => {
      // 详情开关/窗口改变后按实际可用面积重新适配。隐藏页签的 0 尺寸不覆盖
      // 上次尺寸，普通轮询和同尺寸切页签不抹掉手动平移、缩放。
      if (
        el.clientWidth &&
        el.clientHeight &&
        (el.clientWidth !== viewport.current.width ||
          el.clientHeight !== viewport.current.height)
      ) {
        fitCanvas();
      }
    };
    const observer = new ResizeObserver(resize);
    observer.observe(el);
    resize();
    return () => {
      observer.disconnect();
      select(el).on('.zoom', null);
    };
  }, []);
  useEffect(() => {
    const node = graph.nodes.find((n) => n.id === focus?.id),
      el = host.current,
      z = behavior.current;
    if (!node || !el || !z || !el.clientWidth) return;
    // 双向定位只在显式导航时居中；轮询更新不重置用户的视口。
    // 同步尺寸，避免随后同尺寸的 ResizeObserver 通知覆盖这次显式定位。
    viewport.current = { width: el.clientWidth, height: el.clientHeight };
    const k = 1;
    select(el).call(
      z.transform,
      zoomIdentity
        .translate(
          el.clientWidth / 2 - (node.x + node.width / 2) * k,
          el.clientHeight / 2 - (node.y + node.height / 2) * k,
        )
        .scale(k),
    );
  }, [focus?.id, focus?.nonce]);
  const changeScale = (factor: number) => {
    if (host.current && behavior.current)
      select(host.current).call(behavior.current.scaleBy, factor);
  };
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  return (
    <>
      <div className="audit-graph-toolbar">
        <span>
          {extra || (
            <span className="audit-muted">实线：实际路径　虚线：未走分支</span>
          )}
        </span>
        <Space size={6}>
          <Button
            size="small"
            aria-label={`${label}缩小`}
            onClick={() => changeScale(0.8)}
          >
            −
          </Button>
          <Button size="small" onClick={fitCanvas}>
            适应画布
          </Button>
          <Button
            size="small"
            aria-label={`${label}放大`}
            onClick={() => changeScale(1.25)}
          >
            ＋
          </Button>
        </Space>
      </div>
      <section
        className="audit-diagram"
        ref={host}
        aria-label={label}
        tabIndex={0}
        onKeyDown={(event) => {
          if (event.target !== host.current) return;
          if (
            ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(
              event.key,
            ) &&
            host.current &&
            behavior.current
          ) {
            event.preventDefault();
            const dx =
              event.key === 'ArrowLeft'
                ? 60
                : event.key === 'ArrowRight'
                  ? -60
                  : 0;
            const dy =
              event.key === 'ArrowUp'
                ? 60
                : event.key === 'ArrowDown'
                  ? -60
                  : 0;
            select(host.current).call(
              behavior.current.translateBy,
              dx / transform.k,
              dy / transform.k,
            );
          }
        }}
      >
        <div
          className="audit-diagram-layer"
          style={{
            width: bounds.current.width,
            height: bounds.current.height,
            // D3 的 toString 用于 SVG；这里是 HTML 容器，平移必须带 CSS 单位。
            transform: `translate(${transform.x}px, ${transform.y}px) scale(${transform.k})`,
          }}
        >
          <svg
            width={bounds.current.width}
            height={bounds.current.height}
            aria-hidden="true"
            className="audit-edges"
          >
            <defs>
              {['active', 'idle'].map((type) => (
                <marker
                  key={type}
                  id={`${marker}-${type}`}
                  markerWidth="8"
                  markerHeight="8"
                  refX="7"
                  refY="4"
                  orient="auto"
                >
                  <path
                    d="M0 0 L8 4 L0 8Z"
                    fill={type === 'active' ? '#3478ff' : '#bac5d7'}
                  />
                </marker>
              ))}
            </defs>
            {graph.edges.map((edge) => {
              const a = byId.get(edge.source),
                b = byId.get(edge.target);
              if (!a || !b) return null;
              const x1 = a.x + a.width,
                y1 = a.y + a.height / 2,
                x2 = b.x,
                y2 = b.y + b.height / 2,
                mid = (x1 + x2) / 2;
              return (
                <g key={`${edge.source}/${edge.target}`}>
                  <path
                    className={edge.running ? 'audit-edge-running' : undefined}
                    d={`M${x1} ${y1} H${mid} V${y2} H${x2}`}
                    fill="none"
                    stroke={edge.active ? '#3478ff' : '#bac5d7'}
                    strokeWidth={edge.running ? 3 : 2}
                    strokeDasharray={
                      edge.running ? '8 5' : edge.active ? undefined : '5 5'
                    }
                    markerEnd={`url(#${marker}-${edge.active ? 'active' : 'idle'})`}
                  />
                  {edge.label && (
                    <text
                      x={mid}
                      y={(y1 + y2) / 2 - 12}
                      textAnchor="middle"
                      paintOrder="stroke"
                      stroke="#f8faff"
                      strokeWidth={4}
                      fill={edge.active ? '#2460ed' : '#8291a9'}
                      fontSize="12"
                    >
                      {edge.label}
                    </text>
                  )}
                </g>
              );
            })}
          </svg>
          {graph.nodes.map((node) => (
            <button
              key={node.id}
              type="button"
              aria-pressed={selected === node.id}
              className={`audit-graph-node ${node.tone || ''} ${node.diamond ? 'decision' : ''} ${selected === node.id ? 'selected' : ''} node-${node.status}`}
              style={{
                left: node.x,
                top: node.y,
                width: node.width,
                height: node.height,
              }}
              title={[
                node.title,
                node.label || statusText(node.status),
                node.subtitle,
              ]
                .filter(Boolean)
                .join(' · ')}
              onClick={() => onSelect(node.id)}
            >
              <strong>{node.title}</strong>
              <span className={`audit-node-status stage-${node.status}`}>
                {node.status === 'running' && (
                  <i className="audit-running-dot" aria-hidden="true" />
                )}
                {node.label || statusText(node.status)}
              </span>
              {node.subtitle && <small>{node.subtitle}</small>}
            </button>
          ))}
        </div>
      </section>
    </>
  );
}
