import { describe, expect, it } from 'vitest';
import type { AuditFile, ReportData, Span } from '@/services/auditTasks';
import {
  agentGraph,
  ancestors,
  flowGraph,
  overviewText,
  reportBranch,
  skippedAudit,
  stageText,
  traceForest,
} from './graphModel';

const testFile: AuditFile = {
  file_id: 'f',
  file_name: '投标文件.docx',
  source_path: '投标文件.docx',
  status: 'completed',
  phase: 'finished',
  project_id: 'p',
  project_name: '采购要求',
  bidder_id: 'b',
  bidder_name: '投标证明材料',
  stages: ['parsing', 'routing', 'indexing', 'auditing', 'exporting'].map(
    (phase) => ({ phase, status: 'completed', reason: null }),
  ),
  route: { match_status: 'matched', match_reason: '属于同一项目' },
  word_status: 'ready',
  error: null,
  export_error: null,
  report_kind: 'final',
  report_revision: 'r',
  overall_conclusion: 'manual_review',
  outputs: { json_url: '/json', md_url: '/md', word_url: '/word' },
};
const node = (
  id: string,
  kind: string,
  parent: string | null = null,
  actor = 'bid_agent',
  extra: Partial<Span> = {},
): Span => ({
  span_id: id,
  parent_span_id: parent,
  kind,
  actor,
  file_id: 'f',
  batch_index: 1,
  turn_index: null,
  name: kind,
  title: kind,
  tool_call_id: null,
  status: 'completed',
  started_at: '2026-09-30T10:00:00Z',
  finished_at: '2026-09-30T10:01:00Z',
  duration_ms: 1000,
  summary: '',
  has_detail: true,
  ...extra,
});
const sample = () => [
  node('file', 'file', null, 'system'),
  node('audit', 'auditing', 'file'),
  node('main', 'batch', 'audit'),
  node('t', 'turn', 'main'),
  node('search1', 'tool', 't', 'bid_agent', {
    tool_name: 'search_documents',
    status: 'failed',
  }),
  node('search2', 'tool', 't', 'bid_agent', { tool_name: 'search_documents' }),
  node('delegate1', 'tool', 't', 'bid_agent', {
    tool_name: 'request_evidence_review',
  }),
  node('check1', 'evidence_check', 'delegate1', 'evidence_subagent', {
    check_index: 1,
    rule_ids: ['rule-147', 'rule-148'],
    session_id: 'inherited-main',
  }),
  node('child1', 'batch', 'check1', 'evidence_subagent'),
  node('ct', 'turn', 'child1', 'evidence_subagent'),
  node('cs', 'tool', 'ct', 'evidence_subagent', {
    tool_name: 'search_documents',
  }),
  node('cm', 'model', 'ct', 'evidence_subagent', {
    tool_name: 'request_evidence_review',
  }),
  node('delegate2', 'tool', 't', 'bid_agent', {
    tool_name: 'request_evidence_review',
  }),
  node('check2', 'evidence_check', 'delegate2', 'evidence_subagent', {
    check_index: 1,
    rule_ids: ['rule-147'],
  }),
  node('child2', 'batch', 'check2', 'evidence_subagent'),
];
describe('三视图的真实数据投影', () => {
  it('主子沙箱分别聚合，高亮次数沿用实际调用', () => {
    const graph = agentGraph([...sample(),
      node('python-main', 'tool', 't', 'bid_agent', { tool_name: 'run_python', status: 'running' }),
      node('python-child', 'tool', 'ct', 'evidence_subagent', { tool_name: 'run_python', status: 'failed' }),
    ]);
    expect(graph.spanToNode.get('python-main')).toBe('main-python');
    expect(graph.spanToNode.get('python-child')).toBe('evidence-python');
    expect(graph.nodes.find((n) => n.id === 'main-python')).toMatchObject({title: '沙箱工具', status: 'running', members: ['python-main']});
    expect(graph.nodes.find((n) => n.id === 'evidence-python')).toMatchObject({status: 'failed', members: ['python-child']});
  });
  it('不同状态共用固定流程坐标；completed 不覆盖 skipped', () => {
    const file = {
      ...testFile,
      route: { match_status: 'not_matched', match_reason: '项目不同' },
      stages: testFile.stages.map((s) =>
        ['indexing', 'auditing'].includes(s.phase)
          ? { ...s, status: 'skipped' }
          : s,
      ),
    };
    const matched = flowGraph(testFile, []),
      rejected = flowGraph(file, []);
    expect(rejected.nodes.map((n) => [n.id, n.x, n.y])).toEqual(
      matched.nodes.map((n) => [n.id, n.x, n.y]),
    );
    expect(rejected.nodes.find((n) => n.id === 'auditing')?.status).toBe(
      'skipped',
    );
    expect(
      rejected.edges
        .filter((e) => e.active)
        .map((e) => `${e.source}/${e.target}`),
    ).toContain('not_matched/exporting');
    expect(rejected.edges.find((e) => e.target === 'indexing')?.active).toBe(
      false,
    );
    expect(stageText(file, 'routing')).toBe('不匹配');
    expect(overviewText(file)).toContain('未进入规则审核');
    expect(skippedAudit(file)).toBe(true);
  });
  it('uncertain 与不匹配分支分开，缺阶段不自动补完成', () => {
    const file = {
      ...testFile,
      route: { match_status: 'uncertain', match_reason: '编号缺失' },
      stages: [],
    };
    const graph = flowGraph(file, []);
    expect(graph.edges.find((e) => e.target === 'uncertain')?.active).toBe(
      true,
    );
    expect(stageText(file, 'auditing')).toBe('未记录');
    expect(skippedAudit(file)).toBe(false);
  });
  it('解析失败不点亮后续报告，耗时不虚构', () => {
    const file = {
      ...testFile,
      status: 'failed',
      route: null,
      report_kind: 'none' as const,
      word_status: 'unavailable',
      stages: [{ phase: 'parsing', status: 'failed', reason: '不可解析' }],
    };
    expect(flowGraph(file, []).edges.filter((e) => e.active)).toEqual([
      { source: 'file', target: 'parsing', active: true },
    ]);
    expect(flowGraph(file, []).nodes.every((n) => !n.members.length)).toBe(
      true,
    );
  });
  it('普通工具按最近真实会话聚合，子会话不串主会话；失败调用仍保留', () => {
    const graph = agentGraph(sample());
    const group = graph.nodes.find((n) => n.id === 'main-search');
    expect(group?.members).toEqual(['search1', 'search2']);
    expect(group?.subtitle).toBe('调用 2 次 · 曾失败 1 次');
    expect(group?.status).toBe('completed');
    expect(
      graph.nodes.find((n) => n.id === 'evidence-search')?.members,
    ).toEqual(['cs']);
    expect(graph.nodes.find((n) => n.id === 'main-agent')?.status).toBe(
      'completed',
    );
    expect(graph.spanToNode.get('cm')).toBe('evidence-agent');
  });
  it('同类能力固定聚合，重复委派保留独立成员与事项定位', () => {
    const graph = agentGraph(sample());
    expect(graph.nodes.find((n) => n.id === 'delegate')?.members).toEqual([
      'delegate1',
      'delegate2',
    ]);
    expect(graph.spanToNode.get('check1')).toBe('evidence-agent');
    expect(graph.spanToNode.get('check2')).toBe('evidence-agent');
    expect(graph.edges).toContainEqual(
      expect.objectContaining({
        source: 'delegate',
        target: 'evidence-agent',
        active: true,
      }),
    );
    expect(graph.nodes).toHaveLength(10);
    expect(graph.nodes.some((n) => n.id === 'cm')).toBe(false);
  });
  it('模型/轮次定位到所属会话，工具定位到所属聚合节点', () => {
    const graph = agentGraph(sample());
    expect(graph.spanToNode.get('t')).toBe('main-agent');
    expect(graph.spanToNode.get('cs')).toBe('evidence-search');
  });
  it('首次加载即展示固定结构，增加调用不增加节点或改变坐标', () => {
    const empty = agentGraph([]);
    const populated = agentGraph(sample());
    expect(empty.nodes.map((n) => [n.id, n.x, n.y])).toEqual(
      populated.nodes.map((n) => [n.id, n.x, n.y]),
    );
    expect(
      empty.nodes.every((n) => n.status === 'inactive' && !n.members.length),
    ).toBe(true);
    expect(empty.edges.every((e) => !e.active && !e.running)).toBe(true);
  });
  it('模型、工具、下一轮模型在固定节点间切换高亮，不把等待的父会话常亮', () => {
    const main = node('main', 'batch', null, 'bid_agent', {
      status: 'running',
    });
    const model = node('model', 'model', 'main', 'bid_agent', {
      status: 'running',
    });
    const first = agentGraph([main, model]);
    expect(
      first.nodes.filter((n) => n.status === 'running').map((n) => n.id),
    ).toEqual(['main-agent']);
    const search = node('search', 'tool', 'main', 'bid_agent', {
      status: 'running',
      tool_name: 'search_documents',
    });
    const next = agentGraph([main, { ...model, status: 'completed' }, search]);
    expect(
      next.nodes.filter((n) => n.status === 'running').map((n) => n.id),
    ).toEqual(['main-search']);
    expect(next.nodes.find((n) => n.id === 'main-agent')?.label).toBe(
      '等待工具返回',
    );
    expect(next.edges.filter((e) => e.running).map((e) => e.target)).toEqual([
      'main-search',
    ]);
    const last = agentGraph([
      main,
      { ...model, status: 'completed' },
      { ...search, status: 'completed' },
      { ...model, span_id: 'model2' },
    ]);
    expect(
      last.nodes.filter((n) => n.status === 'running').map((n) => n.id),
    ).toEqual(['main-agent']);
    expect(last.nodes.find((n) => n.id === 'main-agent')?.subtitle).toBe(
      '模型调用 2 次',
    );
    expect(first.nodes.map((n) => [n.id, n.x, n.y])).toEqual(
      last.nodes.map((n) => [n.id, n.x, n.y]),
    );
  });
  it('默认展示进行中或最近主会话，保留用户明确选择', () => {
    const spans = [
      ...sample(),
      node('main2', 'batch', 'audit'),
      node('main3', 'batch', 'audit'),
    ];
    expect(agentGraph(spans).session?.span_id).toBe('main3');
    spans[spans.length - 2].status = 'running';
    expect(agentGraph(spans).session?.span_id).toBe('main2');
    expect(agentGraph(spans, 'main').session?.span_id).toBe('main');
  });
  it('父节点晚到才连接；最终缺父及循环归到未连接记录', () => {
    const spans = sample(),
      late = spans.filter((s) => s.span_id !== 'delegate1');
    expect(agentGraph(late).spanToNode.has('child1')).toBe(false);
    expect(agentGraph(late).detached.map((s) => s.span_id)).toContain('cs');
    expect(agentGraph(spans).spanToNode.get('child1')).toBe('evidence-agent');
    const cycle = [node('a', 'batch', 'b'), node('b', 'turn', 'a')];
    expect(traceForest(cycle).detached).toHaveLength(2);
    expect(
      ancestors(cycle[0], new Map(cycle.map((s) => [s.span_id, s]))),
    ).toHaveLength(1);
  });
  it('乱序到达也以真实主会话为布局根；不同主会话隔离', () => {
    const spans = sample();
    const root = spans.find((s) => s.span_id === 'main');
    if (!root) throw new Error('测试主会话缺失');
    const shuffled = [
      ...spans.filter((s) => s !== root),
      root,
      node('main2', 'batch', 'audit'),
      node('search3', 'tool', 'main2', 'bid_agent', {
        tool_name: 'search_documents',
      }),
    ];
    const first = agentGraph(shuffled, 'main'),
      second = agentGraph(shuffled, 'main2');
    expect(first.sessions).toHaveLength(2);
    expect(first.nodes.find((n) => n.id === 'main-agent')?.x).toBe(30);
    expect(first.nodes.some((n) => n.members.includes('search3'))).toBe(false);
    expect(second.nodes.some((n) => n.members.includes('search1'))).toBe(false);
  });
});
describe('说明报告结果边界', () => {
  const report: ReportData = {
    file: testFile,
    document: { kind: 'final', markdown: '报告', revision: 'r' },
    result: {
      is_applicable: false,
      overall_conclusion: 'not_applicable',
      audit_summary: null,
      project_match: { status: 'not_matched', reason: '项目不同' },
      exclusion_reason: '项目不同',
      category_summaries: {},
      findings: [],
      suggestions: [],
      sources: {},
    },
    rules: [],
    counts: null,
    source_links: [],
  };
  it('结果缺失/相互冲突不当作不匹配', () => {
    expect(reportBranch({ ...report, result: null })).toBe('invalid');
    expect(reportBranch(report)).toBe('invalid');
    expect(
      reportBranch({
        ...report,
        file: {
          ...testFile,
          route: { match_status: 'not_matched', match_reason: '项目不同' },
        },
      }),
    ).toBe('not_matched');
  });
  it('草稿不是正式结果，Word失败不取消正式结果', () => {
    if (!report.result) throw new Error('测试结果缺失');
    expect(
      reportBranch({
        ...report,
        document: { ...report.document, kind: 'draft' },
      }),
    ).toBe('pending');
    expect(
      reportBranch({
        ...report,
        file: {
          ...testFile,
          word_status: 'failed',
          route: { match_status: 'uncertain', match_reason: '未知' },
        },
        result: {
          ...report.result,
          project_match: { status: 'uncertain', reason: '未知' },
          overall_conclusion: 'manual_review',
        },
      }),
    ).toBe('uncertain');
  });
});
