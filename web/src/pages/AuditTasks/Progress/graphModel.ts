import { hierarchy, tree } from 'd3';
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
    return file.error?.message || '处理失败，请查看失败阶段';
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
  collapsible?: boolean;
  collapsed?: boolean;
};
export type GraphEdge = {
  source: string;
  target: string;
  active?: boolean;
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
export function agentGraph(
  spans: Span[],
  requestedSession?: string,
  collapsed: ReadonlySet<string> = new Set(),
) {
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
  let nodes: GraphNode[] = [];
  const spanToNode = new Map<string, string>();
  if (!session)
    return { nodes, edges: [], sessions, session, spanToNode, detached };
  const inSession = spans.filter(
    (s) =>
      !detachedIds.has(s.span_id) &&
      (s === session || ancestors(s, byId).includes(session)),
  );
  const anchor = (s: Span) =>
    s.kind === 'batch' ||
    s.kind === 'evidence_check' ||
    (s.kind === 'tool' &&
      (s.tool_name || s.name) === 'request_evidence_review');
  const owner = (s: Span) => ancestors(s, byId).find(anchor);
  for (const s of inSession.filter(anchor)) {
    const ownSpans = inSession.filter(
      (n) => ancestors(n, byId).find((p) => p.kind === 'batch') === s,
    );
    const failed = ownSpans.filter(
      (n) => n.kind === 'tool' && failure(n.status),
    ).length;
    const turns = ownSpans.filter((n) => n.kind === 'turn').length;
    const models = ownSpans.filter((n) => n.kind === 'model').length;
    nodes.push({
      id: s.span_id,
      title: spanTitle(s, spans),
      subtitle:
        s.kind === 'batch'
          ? `${turns} 轮 · 模型调用 ${models} 次${failed ? ` · 含 ${failed} 次失败调用` : ''}`
          : duration(s),
      status: s.status,
      parent: s === session ? undefined : owner(s)?.span_id,
      members: [s.span_id],
      kind: s.kind,
      tone:
        s.actor === 'evidence_subagent' || s.kind === 'evidence_check'
          ? 'purple'
          : 'blue',
      x: 0,
      y: 0,
      width: 210,
      height: 104,
    });
    spanToNode.set(s.span_id, s.span_id);
  }
  for (const s of inSession.filter((s) => s.kind === 'tool' && !anchor(s))) {
    const nearest = ancestors(s, byId).find((p) => p.kind === 'batch');
    if (!nearest || !spanToNode.has(nearest.span_id)) continue;
    const name = s.tool_name || s.name;
    const id = `tool:${nearest.span_id}:${s.actor}:${name}`;
    let node = nodes.find((n) => n.id === id);
    if (!node) {
      node = {
        id,
        title: tools[name] || s.title,
        subtitle: '',
        status: s.status,
        parent: nearest.span_id,
        members: [],
        kind: 'tool_group',
        tone: s.actor === 'evidence_subagent' ? 'purple' : 'blue',
        x: 0,
        y: 0,
        width: 210,
        height: 104,
      };
      nodes.push(node);
    }
    node.members.push(s.span_id);
    spanToNode.set(s.span_id, id);
  }
  for (const node of nodes.filter((n) => n.kind === 'tool_group')) {
    const members = node.members.flatMap((id) => {
      const value = byId.get(id);
      return value ? [value] : [];
    });
    const failures = members.filter((s) => failure(s.status)).length;
    node.subtitle = `共 ${members.length} 次${failures ? ` · 失败 ${failures} 次` : ''}`;
    node.status = members.some((s) => s.status === 'running')
      ? 'running'
      : failures
        ? 'failed'
        : members.every((s) => ['completed', 'succeeded'].includes(s.status))
          ? 'completed'
          : 'unknown';
  }
  // 折叠只隐藏显示节点，不改变调用计数、原始 span 和双向定位映射。
  const allNodes = new Map(nodes.map((n) => [n.id, n]));
  const parents = new Set(nodes.map((n) => n.parent));
  for (const node of nodes) {
    node.collapsible =
      node.kind === 'batch' &&
      byId.get(node.id)?.actor === 'evidence_subagent' &&
      parents.has(node.id);
    node.collapsed = node.collapsible && collapsed.has(node.id);
  }
  nodes = nodes.filter((node) => {
    let parent = node.parent ? allNodes.get(node.parent) : undefined;
    while (parent) {
      if (parent.collapsed) return false;
      parent = parent.parent ? allNodes.get(parent.parent) : undefined;
    }
    return true;
  });
  // D3 只计算布局，节点身份及父子关系均来自上面的真实投影。
  const childMap = new Map<string, GraphNode[]>();
  for (const node of nodes)
    if (node.parent)
      childMap.set(node.parent, [...(childMap.get(node.parent) || []), node]);
  const rootNode = nodes.find((n) => n.id === session.span_id);
  if (!rootNode) throw new Error('主审核会话投影缺失');
  const root = tree<GraphNode>().nodeSize([140, 270])(
    hierarchy(rootNode, (n) => childMap.get(n.id)),
  );
  const minX = Math.min(...root.descendants().map((n) => n.x));
  for (const n of root.descendants()) {
    n.data.x = n.y + 28;
    n.data.y = n.x - minX + 36;
  }
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
      n.parent ? [{ source: n.parent, target: n.id, active: true }] : [],
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
