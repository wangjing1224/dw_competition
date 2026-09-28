import type { Settings as LayoutSettings } from '@ant-design/pro-components';
import type { RequestConfig, RunTimeLayoutConfig } from '@umijs/max';
import { Link } from '@umijs/max';
import dayjs from 'dayjs';
import relativeTime from 'dayjs/plugin/relativeTime';
import defaultSettings from '../config/defaultSettings';

dayjs.extend(relativeTime);

// 保留模板页面的类型兼容，但本工作台不请求用户接口、不执行登录跳转。
export async function getInitialState(): Promise<{
  settings?: Partial<LayoutSettings>;
  currentUser?: API.CurrentUser;
  fetchUserInfo?: () => Promise<API.CurrentUser | undefined>;
  settingDrawerOpen?: boolean;
}> {
  return { settings: defaultSettings as Partial<LayoutSettings> };
}

export const layout: RunTimeLayoutConfig = () => ({
  ...defaultSettings,
  menu: { locale: false },
  siderWidth: 224,
  logo: <span className="brand-mark">审</span>,
  // side 布局会把 actions 放进侧栏；收起时不输出长文案，避免挤成竖排。
  actionsRender: ({ collapsed }) =>
    collapsed
      ? []
      : [
          <span key="label" className="header-note">
            招投标智能审核 · 规则 · 资料 · 审核
          </span>,
        ],
  avatarProps: undefined,
  footerRender: false,
  menuItemRender: (item, dom) =>
    item.disabled ? (
      <span title="后续开放">{dom}</span>
    ) : (
      <Link to={item.path || '/home'}>{dom}</Link>
    ),
  menuFooterRender: (props) =>
    props?.collapsed ? null : (
      <div className="sidebar-note">
        智能整理 · 依据可查
        <br />
        人工审阅 · 规则可复用
      </div>
    ),
});
export const request: RequestConfig = { baseURL: '' };
