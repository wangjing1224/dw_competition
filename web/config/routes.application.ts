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
  {
    path: '/evidence-libraries',
    name: '投标方资料库',
    icon: 'FolderOutlined',
    component: './EvidenceLibraries/List',
  },
  {
    path: '/evidence-libraries/new',
    name: '新建资料库',
    hideInMenu: true,
    component: './EvidenceLibraries/Create',
  },
  {
    path: '/evidence-libraries/:bidderId/progress',
    name: '资料整理进度',
    hideInMenu: true,
    component: './EvidenceLibraries/Progress',
  },
  {
    path: '/evidence-libraries/:bidderId',
    name: '资料审阅',
    hideInMenu: true,
    component: './EvidenceLibraries/Detail',
  },
  {
    path: '/audit-tasks',
    name: '审核任务',
    icon: 'AuditOutlined',
    component: './AuditTasks/List',
  },
  {
    path: '/audit-tasks/new',
    name: '新建审核任务',
    hideInMenu: true,
    component: './AuditTasks/Create',
  },
  {
    path: '/audit-tasks/:taskId/progress',
    name: '审核执行',
    hideInMenu: true,
    component: './AuditTasks/Progress',
  },
  {
    path: '/audit-tasks/:taskId/report',
    name: '审核报告',
    hideInMenu: true,
    component: './AuditTasks/Report',
  },
  { path: '*', component: './exception/404', layout: false },
];
