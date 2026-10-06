import type { AuditFile, ReportData, Span } from '@/services/auditTasks';
import { stageStates, statusLabels } from '@/services/auditTasks';

export const phases = [
  ['parsing', '文档解析'],
  ['routing', '适用性判断'],
  ['indexing', '原文索引'],
  ['auditing', '规则审核'],
  ['exporting', '报告输出'],
] as const;
export const actors: Record<string, string> = {
  system: '系统',
  router: '适用性判断',
  bid_agent: '主审核 Agent',
  evidence_subagent: '资料核验 Agent',
};
const tools: Record<string, string> = {
  search_documents: '原文检索',
  view_image: '查看图片',
  request_evidence_review: '资料核验委派',
  report_document: '报告维护',
  run_python: '沙箱工具',
};
export const failure = (status: string) =>
  ['failed', 'interrupted'].includes(status);
export const statusText = (status: string) =>
  ({
    completed: '已完成',
    succeeded: '已完成',
    running: '进行中',
    unknown: '未记录',
    inactive: '未走分支',
  })[status] ||
  stageStates[status] ||
  statusLabels[status] ||
  status;
export const duration = (span: Span) =>
  span.duration_ms == null
    ? span.status === 'running'
      ? '进行中'
      : '未记录耗时'
    : `${(span.duration_ms / 1000).toFixed(2)}s`;
export function stage(file: AuditFile, phase: string) {
  const found = file.stages.find((s) => s.phase === phase);
  return {
    phase,
    reason: found?.reason || null,
    status: found?.status || 'unknown',
  };
}
export function skippedAudit(file: AuditFile) {
  return (
    ['not_matched', 'uncertain'].includes(file.route?.match_status || '') &&
    stage(file, 'auditing').status === 'skipped'
  );
}
export function outputText(file: AuditFile) {
  const report =
    file.report_kind === 'final'
      ? '正式结果已就绪'
      : file.report_kind === 'draft'
        ? '已有报告草稿'
        : '尚无报告';
  const word: Record<string, string> = {
    ready: 'Word 已生成',
    generating: 'Word 生成中',
    failed: 'Word 导出失败',
  };
  return `${report}${word[file.word_status] ? ` · ${word[file.word_status]}` : ''}`;
}
export function overviewText(file: AuditFile) {
  if (file.error || file.status === 'failed')
    return file.report_kind === 'draft'
      ? '处理未完成，已保存草稿可供查看'
      : file.report_kind === 'final'
        ? '处理出现异常，已保存的正式结果仍可查看'
        : '处理未完成，尚未生成报告';
  if (!['completed', 'completed_with_report_error'].includes(file.status)) {
    return file.status === 'queued'
      ? '文件已接收，等待处理'
      : `当前阶段：${phases.find(([p]) => p === file.phase)?.[1] || '处理中'}`;
  }
  if (skippedAudit(file))
    return `${file.route?.match_status === 'uncertain' ? '项目适用性需人工判断' : '项目不匹配'}，未进入规则审核；${outputText(file)}`;
  return `${stage(file, 'auditing').status === 'completed' ? '已完成规则审核' : '文件处理已结束'}；${outputText(file)}`;
}
export function stageText(file: AuditFile, phase: string) {
  const s = stage(file, phase);
  if (phase === 'routing' && s.status === 'completed')
    return (
      { matched: '匹配', not_matched: '不匹配', uncertain: '需人工判断' }[
        file.route?.match_status || ''
      ] || '完成'
    );
  if (phase === 'exporting' && file.word_status === 'ready')
    return 'Word 已生成';
  return statusText(s.status);
}
export type GraphNode = {
  id: string;
  title: string;
  subtitle: string;
  status: string;
  label?: string;
  x: number;
  y: number;
  width: number;
  height: number;
  tone?: 'blue' | 'purple' | 'orange';
  diamond?: boolean;
  members: string[];
  parent?: string;
  kind?: string;
};
export type GraphEdge = {
  source: string;
  target: string;
  active?: boolean;
  running?: boolean;
  label?: string;
};
export type Graph = { nodes: GraphNode[]; edges: GraphEdge[] };

export function flowGraph(file: AuditFile, spans: Span[]): Graph {
  const route = file.route?.match_status;
  const phaseNode = (
    id: string,
    title: string,
    x: number,
    y: number,
  ): GraphNode => ({
    id,
    title,
    x,
    y,
    width: 180,
    height: 96,
    status: stage(file, id).status,
    label: stageText(file, id),
    subtitle: stage(file, id).reason || '',
    members: spans
      .filter((s) => s.kind === (id === 'exporting' ? 'export' : id))
      .map((s) => s.span_id),
    kind: 'phase',
  });
  const nodes: GraphNode[] = [
    {
      id: 'file',
      title: file.file_name,
      subtitle: '投标文件',
      status: file.status,
      x: 20,
      y: 214,
      width: 180,
      height: 96,
      members: spans.filter((s) => s.kind === 'file').map((s) => s.span_id),
    },
    phaseNode('parsing', '文档解析', 230, 214),
    {
      ...phaseNode('routing', '项目适用性判断', 440, 214),
      subtitle: file.route?.match_reason || '',
      diamond: true,
      tone: route && route !== 'matched' ? 'orange' : 'blue',
    },
    phaseNode('indexing', '建立原文索引', 660, 50),
    { ...phaseNode('auditing', '主审核 Agent', 880, 50), tone: 'blue' },
    {
      id: 'not_matched',
      title: '不适用说明',
      subtitle: route === 'not_matched' ? file.route?.match_reason || '' : '',
      status: route === 'not_matched' ? 'completed' : 'inactive',
      label: route === 'not_matched' ? '项目不匹配' : '未走分支',
      x: 740,
      y: 214,
      width: 190,
      height: 96,
      members: [],
      tone: 'orange',
    },
    {
      id: 'uncertain',
      title: '人工判断说明',
      subtitle: route === 'uncertain' ? file.route?.match_reason || '' : '',
      status: route === 'uncertain' ? 'completed' : 'inactive',
      label: route === 'uncertain' ? '适用性待判断' : '未走分支',
      x: 740,
      y: 378,
      width: 190,
      height: 96,
      members: [],
      tone: 'orange',
    },
    {
      ...phaseNode('exporting', '报告输出', 1100, 214),
      subtitle: outputText(file),
    },
  ];
  const ran = (phase: string) =>
    ['running', 'completed', 'failed', 'interrupted'].includes(
      stage(file, phase).status,
    );
  return {
    nodes,
    edges: [
      { source: 'file', target: 'parsing', active: ran('parsing') },
      { source: 'parsing', target: 'routing', active: ran('routing') },
      {
        source: 'routing',
        target: 'indexing',
        label: '匹配',
        active: route === 'matched' && ran('indexing'),
      },
      {
        source: 'indexing',
        target: 'auditing',
        active: route === 'matched' && ran('auditing'),
      },
      {
        source: 'auditing',
        target: 'exporting',
        active: route === 'matched' && file.report_kind === 'final',
      },
      {
        source: 'routing',
        target: 'not_matched',
        label: '不匹配',
        active: route === 'not_matched',
      },
      {
        source: 'routing',
        target: 'uncertain',
        label: '无法确定',
        active: route === 'uncertain',
      },
      ...['not_matched', 'uncertain'].map((source) => ({
        source,
        target: 'exporting',
        active: route === source && file.report_kind === 'final',
      })),
    ],
  };
}

/** 仅沿真实父链找归属。缺父节点/环都不能通过继承的 session_id 补造关系。 */
export function ancestors(span: Span, byId: Map<string, Span>) {
  const result: Span[] = [];
  const seen = new Set([span.span_id]);
  let id = span.parent_span_id;
  while (id && byId.has(id) && !seen.has(id)) {
    seen.add(id);
    const parent = byId.get(id);
    if (!parent) break;
    result.push(parent);
    id = parent.parent_span_id;
  }
  return result;
}
export function traceForest(spans: Span[]) {
  const byId = new Map(spans.map((s) => [s.span_id, s]));
  const children = new Map<string, Span[]>();
  const detached: Span[] = [];
  for (const span of spans) {
    const chain = ancestors(span, byId);
    const last = chain.at(-1) || span;
    if (last.parent_span_id) {
      detached.push(span);
      continue;
    }
    const parent = span.parent_span_id || '';
    children.set(parent, [...(children.get(parent) || []), span]);
  }
  return { byId, children, detached };
}
export function spanTitle(span: Span, spans: Span[]) {
  if (span.kind === 'tool')
    return tools[span.tool_name || span.name] || span.title;
  if (span.kind === 'evidence_check')
    return `核验事项 ${String(spans.filter((s) => s.kind === 'evidence_check').findIndex((s) => s.span_id === span.span_id) + 1).padStart(2, '0')}`;
  if (span.kind === 'batch')
    return `${span.actor === 'bid_agent' ? '主审核' : '资料核验'}会话 ${String(spans.filter((s) => s.kind === 'batch' && s.actor === span.actor).findIndex((s) => s.span_id === span.span_id) + 1).padStart(2, '0')}`;
  return span.title;
}
// 固定能力拓扑对应主审核的五个工具与资料子 Agent 的三个工具。
// 节点是能力，不是单次 span；实际调用仅更新成员、状态和次数，不改变布局。
const agentTopology = [
  {
    id: 'main-agent',
    title: '主审核 Agent',
    actor: 'bid_agent',
    x: 30,
    y: 260,
  },
  {
    id: 'main-search',
    title: '原文检索',
    actor: 'bid_agent',
    tool: 'search_documents',
    parent: 'main-agent',
    x: 300,
    y: 20,
  },
  {
    id: 'main-image',
    title: '查看图片',
    actor: 'bid_agent',
    tool: 'view_image',
    parent: 'main-agent',
    x: 300,
    y: 140,
  },
  {
    id: 'main-report',
    title: '报告维护',
    actor: 'bid_agent',
    tool: 'report_document',
    parent: 'main-agent',
    x: 300,
    y: 260,
  },
  {
    id: 'main-python',
    title: '沙箱工具',
    actor: 'bid_agent',
    tool: 'run_python',
    parent: 'main-agent',
    x: 300,
    y: 380,
  },
  {
    id: 'delegate',
    title: '资料核验委派',
    actor: 'bid_agent',
    tool: 'request_evidence_review',
    parent: 'main-agent',
    x: 300,
    y: 500,
  },
  {
    id: 'evidence-agent',
    title: '资料核验 Agent',
    actor: 'evidence_subagent',
    parent: 'delegate',
    x: 570,
    y: 500,
  },
  {
    id: 'evidence-search',
    title: '原文检索',
    actor: 'evidence_subagent',
    tool: 'search_documents',
    parent: 'evidence-agent',
    x: 840,
    y: 380,
  },
  {
    id: 'evidence-image',
    title: '查看图片',
    actor: 'evidence_subagent',
    tool: 'view_image',
    parent: 'evidence-agent',
    x: 840,
    y: 500,
  },
  {
    id: 'evidence-python',
    title: '沙箱工具',
    actor: 'evidence_subagent',
    tool: 'run_python',
    parent: 'evidence-agent',
    x: 840,
    y: 620,
  },
];
export function agentGraph(spans: Span[], requestedSession?: string) {
  const { byId, detached } = traceForest(spans);
  const detachedIds = new Set(detached.map((s) => s.span_id));
  const sessions = spans.filter(
    (s) =>
      s.kind === 'batch' &&
      s.actor === 'bid_agent' &&
      !detachedIds.has(s.span_id),
  );
  const session =
    sessions.find((s) => s.span_id === requestedSession) ||
    [...sessions].reverse().find((s) => s.status === 'running') ||
    sessions.at(-1);
  const spanToNode = new Map<string, string>();
  const inSession = spans.filter(
    (s) =>
      session &&
      !detachedIds.has(s.span_id) &&
      (s === session || ancestors(s, byId).includes(session)),
  );
  const latestStatus = (members: Span[]) =>
    members.some((s) => s.status === 'running')
      ? 'running'
      : [...members]
          .sort((a, b) => a.started_at.localeCompare(b.started_at))
          .at(-1)?.status || 'inactive';
  const nodes: GraphNode[] = agentTopology.map((template) => {
    const owned = inSession.filter((s) => s.actor === template.actor);
    const members = template.tool
      ? owned.filter(
          (s) => s.kind === 'tool' && (s.tool_name || s.name) === template.tool,
        )
      : owned.filter((s) => s.kind === 'model');
    const batches = owned.filter((s) => s.kind === 'batch');
    const failures = members.filter((s) => failure(s.status)).length;
    let status = latestStatus(members);
    let label = members.length ? statusText(status) : '尚无调用记录';
    if (!template.tool) {
      // batch 在整个循环中都是 running；仅模型真正运行时点亮 Agent。
      // 工具执行时 Agent 等待，不能把父会话常亮当作正在调用模型。
      if (status === 'running') label = '模型调用中';
      else if (owned.some((s) => s.kind === 'tool' && s.status === 'running')) {
        status = 'waiting';
        label = '等待工具返回';
      } else if (batches.some((s) => s.status === 'running')) {
        status = 'waiting';
        label = '等待下一步';
      } else if (batches.length) {
        status = latestStatus(batches);
        label = status === 'completed' ? '已结束' : statusText(status);
      }
    }
    for (const s of members) spanToNode.set(s.span_id, template.id);
    for (const s of template.tool
      ? []
      : owned.filter((s) =>
          ['batch', 'turn', 'evidence_check'].includes(s.kind),
        ))
      spanToNode.set(s.span_id, template.id);
    return {
      ...template,
      width: 220,
      height: 100,
      kind: template.tool ? 'tool_group' : 'agent',
      tone: template.actor === 'evidence_subagent' ? 'purple' : 'blue',
      status,
      label,
      subtitle: `${template.tool ? '调用' : '模型调用'} ${members.length} 次${failures ? ` · 曾失败 ${failures} 次` : ''}`,
      members: (template.tool || members.length ? members : batches).map(
        (s) => s.span_id,
      ),
    };
  });
  // 未映射的事件定位到最近的已知能力；完整单次调用始终保留在轨迹树。
  for (const s of inSession) {
    if (!spanToNode.has(s.span_id)) {
      const parent = ancestors(s, byId).find((p) => spanToNode.has(p.span_id));
      const target = parent ? spanToNode.get(parent.span_id) : undefined;
      if (target) spanToNode.set(s.span_id, target);
    }
  }
  return {
    nodes,
    edges: nodes.flatMap((n) =>
      n.parent
        ? [
            {
              source: n.parent,
              target: n.id,
              active: !!n.members.length,
              running: n.status === 'running',
            },
          ]
        : [],
    ),
    sessions,
    session,
    spanToNode,
    detached,
  };
}

export function reportBranch(
  data: ReportData,
): 'pending' | 'applicable' | 'not_matched' | 'uncertain' | 'invalid' {
  if (data.document.kind !== 'final') return 'pending';
  const result = data.result;
  if (!result) return 'invalid';
  const status = result.project_match.status;
  if (data.file.route && data.file.route.match_status !== status)
    return 'invalid';
  if (result.is_applicable)
    return status === 'matched' ? 'applicable' : 'invalid';
  return status === 'not_matched' || status === 'uncertain'
    ? status
    : 'invalid';
}
