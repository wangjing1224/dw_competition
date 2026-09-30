// @vitest-environment jsdom
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from '@testing-library/react';
import { zoomTransform } from 'd3';
import { afterEach, expect, it, vi } from 'vitest';
import GraphCanvas from './GraphCanvas';
import type { Graph } from './graphModel';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it('详情打开/关闭触发重新适配，同尺寸轮询和隐藏页签不重置手动视口', () => {
  let width = 1100;
  let notify = () => {};
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(
    () => width,
  );
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(450);
  vi.stubGlobal(
    'ResizeObserver',
    class {
      constructor(callback: ResizeObserverCallback) {
        notify = () => callback([], this as unknown as ResizeObserver);
      }
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  const graph: Graph = {
    nodes: [
      {
        id: 'file',
        title: '文件',
        subtitle: '',
        status: 'completed',
        x: 20,
        y: 214,
        width: 180,
        height: 96,
        members: [],
      },
      {
        id: 'report',
        title: '报告',
        subtitle: '',
        status: 'completed',
        x: 1100,
        y: 214,
        width: 180,
        height: 96,
        members: [],
      },
    ],
    edges: [{ source: 'file', target: 'report', active: true }],
  };
  const { rerender } = render(
    <GraphCanvas graph={graph} onSelect={vi.fn()} label="自适应画布" />,
  );
  const host = screen.getByRole('region', { name: '自适应画布' });
  const initial = zoomTransform(host);
  width = 680;
  act(() => notify());
  const narrow = zoomTransform(host);
  expect(narrow.k).toBeLessThan(0.6);
  expect(narrow.x + 1280 * narrow.k).toBeLessThan(width - 20);
  width = 1100;
  act(() => notify());
  expect(zoomTransform(host)).toEqual(initial);
  fireEvent.click(screen.getByRole('button', { name: '自适应画布放大' }));
  const manual = zoomTransform(host);
  act(() => notify());
  width = 0;
  act(() => notify());
  width = 1100;
  act(() => notify());
  expect(zoomTransform(host)).toEqual(manual);
  // 显式从轨迹定位时，要居中目标，不能被后到的同尺寸 observer 覆盖。
  width = 680;
  rerender(
    <GraphCanvas
      graph={graph}
      onSelect={vi.fn()}
      label="自适应画布"
      focus={{ id: 'report', nonce: 1 }}
    />,
  );
  const focused = zoomTransform(host);
  expect(focused.applyX(1190)).toBe(340);
  act(() => notify());
  expect(zoomTransform(host)).toEqual(focused);
  width = 1100;
  rerender(<GraphCanvas graph={graph} onSelect={vi.fn()} label="自适应画布" />);
  act(() => notify());
  expect(zoomTransform(host)).toEqual(initial);
});

it('分支标签在连接通道中间，不贴着目标节点被遮挡', () => {
  const graph: Graph = {
    nodes: [
      {
        id: 'route',
        title: '判断',
        subtitle: '',
        status: 'completed',
        x: 440,
        y: 214,
        width: 180,
        height: 96,
        members: [],
      },
      {
        id: 'index',
        title: '索引',
        subtitle: '',
        status: 'completed',
        x: 660,
        y: 50,
        width: 180,
        height: 96,
        members: [],
      },
    ],
    edges: [{ source: 'route', target: 'index', label: '匹配', active: true }],
  };
  const { container } = render(
    <GraphCanvas graph={graph} onSelect={vi.fn()} label="标签画布" />,
  );
  const label = container.querySelector('svg text');
  expect(label).toHaveAttribute('text-anchor', 'middle');
  expect(Number(label?.getAttribute('y'))).toBeGreaterThan(146);
  expect(Number(label?.getAttribute('y'))).toBeLessThan(214);
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
