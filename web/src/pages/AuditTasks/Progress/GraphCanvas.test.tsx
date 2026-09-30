// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import GraphCanvas from './GraphCanvas';
import type { Graph } from './graphModel';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
it('节点和连线在同一变换层，键盘平移/缩放可用，更新节点不重置视口', () => {
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(1100);
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(450);
  const graph: Graph = {
    nodes: [
      {
        id: 'file',
        title: '投标文件',
        subtitle: '',
        status: 'completed',
        x: 20,
        y: 50,
        width: 180,
        height: 96,
        members: [],
      },
      {
        id: 'parse',
        title: '文档解析',
        subtitle: '',
        status: 'running',
        x: 250,
        y: 50,
        width: 180,
        height: 96,
        members: [],
      },
    ],
    edges: [{ source: 'file', target: 'parse', active: true }],
  };
  const onSelect = vi.fn();
  const { container, rerender } = render(
    <GraphCanvas graph={graph} onSelect={onSelect} label="流程画布" />,
  );
  const layer = container.querySelector<HTMLElement>('.audit-diagram-layer');
  const initial = layer?.style.transform;
  expect(layer?.querySelector('svg path')).toBeTruthy();
  expect(layer?.querySelector('button')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '流程画布放大' }));
  expect(layer?.style.transform).not.toBe(initial);
  const scaled = layer?.style.transform;
  fireEvent.keyDown(screen.getByRole('region', { name: '流程画布' }), {
    key: 'ArrowRight',
  });
  expect(layer?.style.transform).not.toBe(scaled);
  const moved = layer?.style.transform;
  rerender(
    <GraphCanvas
      graph={{
        ...graph,
        nodes: graph.nodes.map((n) => ({ ...n, status: 'completed' })),
      }}
      onSelect={onSelect}
      label="流程画布"
    />,
  );
  expect(layer?.style.transform).toBe(moved);
  fireEvent.click(screen.getByRole('button', { name: /文档解析/ }));
  expect(onSelect).toHaveBeenCalledWith('parse');
  expect(layer?.style.transform).toBe(moved);
  fireEvent.click(screen.getByRole('button', { name: '适应画布' }));
  expect(layer?.style.transform).toBe(initial);
});
