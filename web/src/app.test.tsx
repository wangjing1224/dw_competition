import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';

vi.mock('@umijs/max', () => ({
  Link: ({ children }: { children: ReactNode }) => children,
}));
afterEach(() => vi.unstubAllGlobals());
describe('业务工作台启动边界', () => {
  it('没有用户系统，不查询用户、不因未登录而阻止审核入口', async () => {
    const fetch = vi.fn();
    vi.stubGlobal('fetch', fetch);
    const { getInitialState } = await import('./app');
    const state = await getInitialState();
    expect(state.settings).toBeDefined();
    expect(state.currentUser).toBeUndefined();
    expect(state.fetchUserInfo).toBeUndefined();
    expect(fetch).not.toHaveBeenCalled();
  });
});
