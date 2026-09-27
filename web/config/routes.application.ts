// 模板路由保留在 routes.ts；正式工作台只开放已实现的业务。
export default [
  { path: '/', redirect: '/home' },
  {
    path: '/home',
    name: '工作台',
    icon: 'HomeOutlined',
    component: './Workbench',
  },
  {
    path: '/rule-libraries',
    name: '规则库',
    icon: 'ReadOutlined',
    component: './RuleLibraries/List',
  },
  {
    path: '/rule-libraries/new',
    hideInMenu: true,
    name: '新建规则库',
    component: './RuleLibraries/Create',
  },
  {
    path: '/rule-libraries/:projectId/progress',
    hideInMenu: true,
    name: '抽取进度',
    component: './RuleLibraries/Progress',
  },
  {
    path: '/rule-libraries/:projectId',
    hideInMenu: true,
    name: '规则审阅',
    component: './RuleLibraries/Detail',
  },
  { name: '投标方资料库 · 后续开放', icon: 'FolderOutlined', disabled: true },
  { name: '审核任务 · 后续开放', icon: 'AuditOutlined', disabled: true },
  { path: '*', component: './exception/404', layout: false },
];
