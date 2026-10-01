// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import ExpandableContent from './ExpandableContent';
import { Markdown } from './shared';

vi.mock('@umijs/max', () => ({ Link: () => null, history: { push: vi.fn() } }));
afterEach(cleanup);
it('长正文默认折叠，展开后仍限高滚动且保留全文', () => {
  const text = `**关键结论**\n\n${'详细核验说明。'.repeat(80)}\n\n原始内容末尾`;
  const { container } = render(
    <ExpandableContent text={text} label="总体结论">
      <Markdown text={text} />
    </ExpandableContent>,
  );
  const region = screen.getByRole('region', { name: '总体结论' });
  expect(region).toHaveStyle({ maxHeight: '144px', overflowY: 'hidden' });
  expect(container.querySelector('strong')).toHaveTextContent('关键结论');
  expect(screen.getByText('原始内容末尾')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '展开总体结论' }));
  expect(region).toHaveStyle({ maxHeight: '360px', overflowY: 'auto' });
  expect(screen.getByRole('button', { name: '收起总体结论' })).toHaveAttribute(
    'aria-expanded',
    'true',
  );
  region.scrollTop = 200;
  fireEvent.click(screen.getByRole('button', { name: '收起总体结论' }));
  expect(region).toHaveStyle({ maxHeight: '144px' });
  expect(region.scrollTop).toBe(0);
});
it('短内容不放冗余按钮，异步长内容到达后可展开，Markdown 不执行脚本', () => {
  const { rerender, container } = render(
    <ExpandableContent text="短内容" label="输入">
      <pre>短内容</pre>
    </ExpandableContent>,
  );
  expect(screen.queryByRole('button')).not.toBeInTheDocument();
  const text = `<script>alert(1)</script>\n\n${'后到的长文本。'.repeat(80)}`;
  rerender(
    <ExpandableContent text={text} label="输入">
      <Markdown text={text} />
    </ExpandableContent>,
  );
  expect(screen.getByRole('button', { name: '展开输入' })).toBeInTheDocument();
  expect(container.querySelector('script')).toBeNull();
});
