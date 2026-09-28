// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import { cleanup, render } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import LibraryCounts from './index';

afterEach(cleanup);

it('数字与单位成组，规则数、摘要数共用基线对齐样式', () => {
  const { container, rerender } = render(
    <LibraryCounts fileCount={1} resultCount={71} resultLabel="条规则" />,
  );
  const values = () =>
    Array.from(container.querySelectorAll('.library-metric')).map(
      (item) => item.textContent,
    );
  expect(values()).toEqual(['1份来源文件', '71条规则']);
  rerender(
    <LibraryCounts fileCount={3} resultCount={3} resultLabel="份已整理摘要" />,
  );
  expect(values()).toEqual(['3份来源文件', '3份已整理摘要']);
  rerender(
    <LibraryCounts
      fileCount={3}
      resultCount={null}
      resultLabel="份已整理摘要"
    />,
  );
  expect(values()).toEqual(['3份来源文件', '—份已整理摘要']);
  const css = readFileSync('src/global.less', 'utf8');
  expect(css).toMatch(/\.library-numbers\s*\{[^}]*align-items: baseline/);
  expect(css).toMatch(
    /\.library-metric\s*\{[^}]*align-items: baseline[^}]*white-space: nowrap/,
  );
});
