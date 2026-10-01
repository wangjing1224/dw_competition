import { Alert, Button, Collapse, Empty, Space, Spin, Tag } from 'antd';
import { Failure } from '@/pages/RuleLibraries/shared';
import type {
  AuditFile,
  ReportData,
  Span,
  SpanDetail,
} from '@/services/auditTasks';
import { api } from '@/services/auditTasks';
import { ruleLabels } from '@/utils/displayLabels';
import ExpandableContent from '../ExpandableContent';
import { Markdown, Status, useRemote } from '../shared';
import type { GraphNode } from './graphModel';
import {
  actors,
  duration,
  failure,
  outputText,
  spanTitle,
  statusText,
} from './graphModel';

export type Selection =
  | { type: 'flow' | 'agent' | 'span'; id: string }
  | { type: 'draft'; id: 'draft' };
function VisibleValue({
  value,
  empty,
  label,
  markdown = false,
}: {
  value: unknown;
  empty: string;
  label: string;
  markdown?: boolean;
}) {
  const text =
    value == null
      ? empty
      : typeof value === 'string'
        ? value
        : JSON.stringify(value, null, 2);
  return (
    <ExpandableContent text={text} label={label} previewHeight={128}>
      {markdown && typeof value === 'string' ? (
        <Markdown text={text} />
      ) : (
        <pre>{text}</pre>
      )}
    </ExpandableContent>
  );
}
export default function Inspector({
  task,
  file,
  selection,
  node,
  spans,
  spanRevision,
  onClose,
  onSpan,
  onLocate,
  onAgent,
  replaying = false,
}: {
  task: string;
  file: AuditFile;
  selection: Selection;
  node?: GraphNode;
  spans: Span[];
  spanRevision?: number;
  onClose: () => void;
  onSpan: (id: string) => void;
  onLocate: (id: string) => void;
  onAgent: () => void;
  replaying?: boolean;
}) {
  const span =
    selection.type === 'span'
      ? spans.find((s) => s.span_id === selection.id)
      : undefined;
  const details = useRemote(
    (signal) =>
      span?.has_detail
        ? api<SpanDetail>(`/${task}/trace/${span.span_id}`, { signal })
        : Promise.resolve(null),
    [task, file.file_id, span?.span_id, spanRevision],
    (value) => value?.span.status === 'running',
  );
  const report = useRemote(
    async (signal) => {
      if (selection.type !== 'draft' && !span?.rule_id) return null;
      const data = await api<ReportData>(`/${task}/files/${file.file_id}`, {
        signal,
      });
      if (data.file.file_id !== file.file_id)
        throw new Error('报告归属不符，已阻止展示');
      return data;
    },
    [task, file.file_id, file.report_revision, selection.type, span?.rule_id],
  );
  const memberIds = node?.members || [];
  const verified =
    details.data?.span.file_id === file.file_id &&
    details.data.span.span_id === span?.span_id &&
    (!details.data.span.task_id || details.data.span.task_id === task);
  return (
    <section className="audit-inspection-panel" aria-label="节点详情">
      <header>
        <strong>{selection.type === 'draft' ? '报告草稿' : '节点详情'}</strong>
        <Button type="text" aria-label="关闭节点详情" onClick={onClose}>
          ×
        </Button>
      </header>
      <div className="audit-inspection-body">
        {replaying && (
          <Alert
            type="info"
            title="图中为历史回放状态；单次调用详情是最新已保存记录，不是该时刻的内容快照。"
          />
        )}
        {selection.type === 'draft' ? (
          <>
            {report.error && (
              <Failure error={report.error} retry={report.reload} />
            )}
            {!report.data && !report.error ? (
              <Spin />
            ) : report.data?.document.markdown ? (
              <>
                <p className="audit-muted">
                  {report.data.document.kind === 'final'
                    ? '已定稿'
                    : '未定稿 · 最近一次成功保存'}
                </p>
                <Markdown text={report.data.document.markdown} />
              </>
            ) : (
              <Empty description="尚无已保存正文" />
            )}
          </>
        ) : selection.type === 'span' ? (
          !span ? (
            <Empty description="该调用尚未加载，无法定位详情" />
          ) : (
            <>
              <h3>{spanTitle(span, spans)}</h3>
              <Space wrap>
                <Status value={span.status} />
                <span>{actors[span.actor] || span.actor}</span>
                <small>{duration(span)}</small>
              </Space>
              {span.rule_id && (
                <p>
                  核验规则：
                  {report.data
                    ? ruleLabels(report.data.rules).get(span.rule_id) ||
                      '关联规则，见技术信息'
                    : '关联规则，见技术信息'}
                </p>
              )}
              {!!span.target_file_ids?.length && (
                <p>授权资料：{span.target_file_ids.length} 份</p>
              )}
              <div className="section-gap">
                <Button size="small" onClick={() => onLocate(span.span_id)}>
                  在图中定位
                </Button>
              </div>
              {details.error && (
                <Failure error={details.error} retry={details.reload} />
              )}
              {!span.has_detail ? (
                <p className="audit-muted">此节点未记录独立参数或可见返回。</p>
              ) : !details.data && !details.error ? (
                <Spin />
              ) : details.data && !verified ? (
                <Alert type="error" title="调用详情归属不符，已阻止展示" />
              ) : (
                verified && (
                  <>
                    {details.data?.truncated && (
                      <Alert
                        type="info"
                        title="仅展示已记录的安全内容，部分内容已截断"
                      />
                    )}
                    {details.data?.error && (
                      <Alert type="error" title={details.data.error.message} />
                    )}
                    <h4>调用输入</h4>
                    <VisibleValue
                      value={details.data?.input}
                      empty="未记录输入"
                      label="调用输入"
                    />
                    <h4>可见输出</h4>
                    <VisibleValue
                      value={details.data?.output}
                      empty="尚无可见返回"
                      label="可见输出"
                      markdown
                    />
                  </>
                )
              )}
              <Collapse
                ghost
                items={[
                  {
                    key: 'technical',
                    label: '技术信息',
                    children: (
                      <VisibleValue
                        value={{
                          span_id: span.span_id,
                          session_id: span.session_id,
                          file_id: span.file_id,
                          rule_id: span.rule_id,
                          target_file_ids: span.target_file_ids,
                        }}
                        empty="无"
                        label="技术信息"
                      />
                    ),
                  },
                ]}
              />
            </>
          )
        ) : !node ? (
          <Empty description="当前节点尚未取得" />
        ) : (
          <>
            <h3>{node.title}</h3>
            <Tag
              color={
                failure(node.status)
                  ? 'red'
                  : node.status === 'running'
                    ? 'blue'
                    : node.status === 'completed'
                      ? 'green'
                      : undefined
              }
            >
              {statusText(node.status)}
            </Tag>
            <p className="audit-preserve-text">
              {node.subtitle || node.label || '未记录独立说明'}
            </p>
            {selection.type === 'flow' && selection.id === 'file' && (
              <dl>
                <dt>文件</dt>
                <dd>{file.file_name}</dd>
                <dt>规则库</dt>
                <dd>{file.project_name}</dd>
                <dt>资料库</dt>
                <dd>{file.bidder_name}</dd>
              </dl>
            )}
            {selection.type === 'flow' && selection.id === 'exporting' && (
              <>
                <p>{outputText(file)}</p>
                <p className="audit-muted">
                  使用上方公共进度区查看已保存报告。
                </p>
              </>
            )}
            {selection.type === 'flow' && selection.id === 'auditing' && (
              <Button onClick={onAgent}>查看 Agent 图</Button>
            )}
            {!!memberIds.length && (
              <>
                <h4>
                  {node.kind === 'tool_group' ? '实际调用' : '相关执行记录'}
                </h4>
                <div className="audit-call-members">
                  {memberIds.map((id) => {
                    const s = spans.find((n) => n.span_id === id);
                    return s ? (
                      <div key={id}>
                        <Button type="link" onClick={() => onSpan(id)}>
                          {spanTitle(s, spans)}
                        </Button>
                        <Status value={s.status} />
                        <small>{duration(s)}</small>
                      </div>
                    ) : null;
                  })}
                </div>
                <p className="audit-muted">选择一次实际调用查看参数与结果。</p>
              </>
            )}
            {!memberIds.length && (
              <p className="audit-muted">
                {selection.type === 'agent'
                  ? '这是固定能力节点，尚无已加载调用记录。具体执行情况以轨迹树为准。'
                  : '此处展示业务状态，没有对应的已加载调用记录。'}
              </p>
            )}
          </>
        )}
      </div>
    </section>
  );
}
