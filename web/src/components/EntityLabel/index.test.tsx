// @vitest-environment jsdom
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import EntityLabel from './index';

afterEach(cleanup);

it('默认只显示名称，悬停或聚焦可查看并复制真实 ID', async () => {
  render(<EntityLabel id="project-147">设备采购规则库</EntityLabel>);
  const label = screen.getByText('设备采购规则库');
  expect(screen.queryByText('project-147')).not.toBeInTheDocument();
  expect(label).toHaveAttribute('data-entity-id', 'project-147');
  fireEvent.focus(label);
  expect(await screen.findByRole('tooltip')).toHaveTextContent('project-147');
  expect(screen.getByRole('button', { name: /copy/i })).toBeInTheDocument();
});

it('复制 ID 不触发包裹它的导航按钮', async () => {
  const select = vi.fn();
  render(
    <button type="button" onClick={select}>
      <EntityLabel id="file-38">合同.pdf</EntityLabel>
    </button>,
  );
  fireEvent.mouseEnter(screen.getByText('合同.pdf'));
  await screen.findByRole('tooltip');
  fireEvent.click(screen.getByRole('button', { name: /copy/i }));
  await waitFor(() => expect(select).not.toHaveBeenCalled());
});
