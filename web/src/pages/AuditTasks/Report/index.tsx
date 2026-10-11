import { Link, useParams, useSearchParams } from '@umijs/max';
import {
  Alert,
  Button,
  Card,
  Empty,
  Segmented,
  Select,
  Space,
  Spin,
  Table,
  Tag,
} from 'antd';
import { useCallback, useMemo, useState } from 'react';
import EntityLabel from '@/components/EntityLabel';
import SourcePreview from '@/components/SourcePreview';
import { Failure, PageHeading } from '@/pages/RuleLibraries/shared';
import type { Finding, ReportData, TaskDetail } from '@/services/auditTasks';
import {
  api,
  downloadFile,
  fileUrl,
  orderedFindings,
  resultColors,
  resultLabels,
} from '@/services/auditTasks';
import {
  categories,
  priorities,
  verifications,
} from '@/services/ruleLibraries';
import { fileLabel, ruleLabels } from '@/utils/displayLabels';
import ExpandableContent from '../ExpandableContent';
import { reportBranch } from '../Progress/graphModel';
import {
  Binding,
  Download,
  FileErrors,
  Markdown,
  Status,
  useRemote,
} from '../shared';
import ApplicabilityReport, {
  ExplanationDownloads,
} from './ApplicabilityReport';
import './report.less';
import { highlightDetails } from './presentation';

function Charts({ data }: { data: ReportData }) {
  if (!data.counts || !data.result) return null;
  const counts = data.counts;
  const result = data.result;
  const total = counts.total;
  let start = 0;
  const stops = Object.keys(resultColors).map((k) => {
    const end = start + (total ? (counts[k] / total) * 100 : 0);
    const value = `${resultColors[k]} ${start}% ${end}%`;
    start = end;
    return value;
  });
  return (
    <section className="audit-panel audit-report-summary">
      <div className="audit-report-charts">
        <figure className="audit-report-donut" aria-label="规则结论分布">
          <div
            className="audit-donut"
            style={{
              background: total
                ? `conic-gradient(${stops.join(',')})`
                : '#eef1f7',
            }}
            role="img"
            aria-label={Object.keys(resultColors)
              .map((k) => `${resultLabels[k]} ${counts[k]} 条`)
              .join('，')}
          >
            <div>
              <strong>{total}</strong>
              <span>已核验规则</span>
            </div>
          </div>
          <figcaption className="audit-legend">
            {Object.keys(resultColors).map((k) => (
              <span key={k}>
                <i style={{ background: resultColors[k] }} />
                {resultLabels[k]} {counts[k]}
              </span>
            ))}
          </figcaption>
        </figure>
        <div>
          <strong>规则类别分布</strong>
          {Object.entries(categories).map(([category, label]) => {
            const ids = new Set(
              data.rules
                .filter((r) => r.category === category)
                .map((r) => r.rule_id),
            );
            const findings = result.findings.filter((f) => ids.has(f.rule_id));
            if (!ids.size) return null;
            return (
              <div className="audit-bar-row" key={category}>
                <span>{label}</span>
                <div className="audit-bar">
                  {Object.keys(resultColors).map((k) => (
                    <span
                      key={k}
                      title={`${resultLabels[k]} ${findings.filter((f) => f.result === k).length}`}
                      style={{
                        background: resultColors[k],
                        width: `${findings.length ? (100 * findings.filter((f) => f.result === k).length) / findings.length : 0}%`,
                      }}
                    />
                  ))}
                </div>
                <span>{findings.length}</span>
              </div>
            );
          })}
        </div>
      </div>
      <div className="audit-report-conclusion">
        <span className="audit-muted">当前文件总体结论</span>
        <p className="audit-conclusion">
          {resultLabels[result.overall_conclusion] || result.overall_conclusion}
        </p>
        <ExpandableContent
          text={result.audit_summary || ''}
          label="总体结论"
          previewHeight={168}
        >
          <Markdown text={result.audit_summary || '详见逐条审核说明。'} />
        </ExpandableContent>
      </div>
    </section>
  );
}
function ReportFile({
  taskId,
  fileId,
  files,
  taskName,
  onFile,
}: {
  taskId: string;
  fileId: string;
  files: TaskDetail['files'];
  taskName: string;
  onFile: (id: string) => void;
}) {
  const [params, setParams] = useSearchParams();
  const view = params.get('view') === 'rules' ? 'rules' : 'overview';
  const category = params.get('category') || 'all';
  const conclusion = params.get('result') || 'issues';
  const selected = params.get('rule');
  const highlightIndex = params.get('highlight');
  const { data, error, reload } = useRemote(
    (s) => api<ReportData>(`/${taskId}/files/${fileId}`, { signal: s }),
    [taskId, fileId],
    (d) => !['completed', 'failed'].includes(d.file.status),
  );
  const [source, setSource] = useState<string>();
  const base = `/${taskId}/files/${fileId}`;
  const sourceApi = useCallback(
    <T,>(path: string, init?: RequestInit) => api<T>(base + path, init),
    [base],
  );
  const download = useCallback(
    (path: string, name: string) =>
      downloadFile(`/api/audit-tasks${base}${path}`, name),
    [base],
  );
  const update = (key: string, value: string) => {
    const p = new URLSearchParams(params);
    p.set('file', fileId);
    p.set(key, value);
    setParams(p, { replace: true });
  };
  const map = useMemo(
    () => new Map(data?.rules.map((r) => [r.rule_id, r]) || []),
    [data?.rules],
  );
  const labels = useMemo(() => ruleLabels(data?.rules || []), [data?.rules]);
  const findings = data
    ? orderedFindings(data.result?.findings || [], data.rules)
    : [];
  const activeHighlight = data?.result?.highlights?.find(
    (_, i) => String(i) === highlightIndex,
  );
  const filtered = findings.filter(
    (f) =>
      (!activeHighlight || activeHighlight.rule_ids.includes(f.rule_id)) &&
      (category === 'all' || map.get(f.rule_id)?.category === category) &&
      (conclusion === 'all' ||
        (conclusion === 'issues' && f.result !== 'compliant') ||
        conclusion === f.result),
  );
  const chosen = filtered.find((f) => f.rule_id === selected) || filtered[0];
  const rule = chosen ? map.get(chosen.rule_id) : undefined;
  const keys = chosen
    ? [
        ...new Set([
          ...chosen.tender_chunk_ids,
          ...chosen.bid_chunk_ids,
          ...chosen.evidence_chunk_ids,
          ...chosen.image_paths,
        ]),
      ]
    : [];
  const fileSelector = (
    <Select
      aria-label="切换报告文件"
      className="audit-file-selector"
      value={fileId}
      onChange={onFile}
      options={files.map((f) => ({
        value: f.file_id,
        label: fileLabel(f.file_id, files),
      }))}
    />
  );
  const processLink = (
    <Link to={`${fileUrl(taskId, fileId, 'progress')}&view=flow`}>
      <Button>查看处理过程</Button>
    </Link>
  );
  const finalizedAt =
    data?.file.file_id === fileId && [2, 3].includes(data.result?.report_format_version ?? 1)
      ? data.result?.report_context?.finalized_at
      : null;
  const heading = (
    <PageHeading
      title={data?.result?.report_title || fileLabel(fileId, files)}
      description={
        <span className="audit-report-heading-meta">
          <span className="audit-report-heading-context">
            {data?.result?.report_context?.task_name || taskName} ·{' '}
            {fileLabel(fileId, files)}
          </span>
          {finalizedAt && (
            <span className="audit-report-heading-time">
              报告生成时间：
              <time dateTime={finalizedAt}>
                {new Date(finalizedAt).toLocaleString('zh-CN', {
                  hour12: false,
                })}
              </time>
            </span>
          )}
        </span>
      }
      backTo="/audit-tasks"
      backLabel="返回审核任务"
      actions={data && <Status value={data.file.status} />}
    />
  );
  if (!data)
    return (
      <>
        {heading}
        <div className="audit-toolbar">
          {fileSelector}
          {processLink}
        </div>
        {error ? <Failure error={error} retry={reload} /> : <Spin />}
      </>
    );
  if (data.file.file_id !== fileId)
    return (
      <>
        {heading}
        <div className="audit-toolbar">
          {fileSelector}
          {processLink}
        </div>
        <Alert type="error" title="报告归属不符，已阻止展示" />
      </>
    );
  const branch = reportBranch(data);
  const explanation = branch === 'not_matched' || branch === 'uncertain';
  return (
    <>
      {heading}
      {error && <Failure error={error} retry={reload} />}
      <div className="audit-toolbar">
        {fileSelector}
        <Space wrap>
          <Button onClick={() => setSource(fileId)}>查看解析原文</Button>
          {processLink}
          {explanation ? (
            <ExplanationDownloads data={data} />
          ) : (
            <>
              <Download
                url={data.file.outputs.word_url}
                name={`${data.file.file_name}-审核报告.docx`}
                label="下载 Word"
              />
              <Download
                url={data.file.outputs.md_url}
                name={`${data.file.file_name}-审核报告.md`}
                label="导出 Markdown"
              />
              <Download
                url={data.file.outputs.json_url}
                name={`${data.file.file_name}-审核报告.json`}
                label="导出 JSON"
              />
            </>
          )}
        </Space>
      </div>
      {!explanation && (
        <div className="section-gap">
          <Binding file={data.file} />
        </div>
      )}
      <FileErrors file={data.file} />
      {data.document.kind !== 'final' ? (
        <Card title="已保存内容">
          <Alert
            type="info"
            title={
              data.document.kind === 'draft'
                ? '当前为未定稿草稿，不计入正式结论统计'
                : '尚未生成报告'
            }
          />
          {data.document.markdown ? (
            <Markdown text={data.document.markdown} />
          ) : (
            <Empty description="请查看执行过程与错误详情" />
          )}
        </Card>
      ) : branch === 'invalid' || !data.result ? (
        <Alert
          type="error"
          title="正式结果缺失或状态不一致，无法确认审核结论"
          description="请查看处理过程；不会将数据读取异常判定为项目不匹配。"
        />
      ) : explanation ? (
        <ApplicabilityReport data={data} uncertain={branch === 'uncertain'} />
      ) : (
        <>
          <div className="audit-toolbar">
            <Segmented
              value={view}
              onChange={(v) => update('view', v)}
              options={[
                { label: '结果总览', value: 'overview' },
                { label: '规则明细', value: 'rules' },
              ]}
            />
            <span className="audit-muted">仅使用本次审核固定的规则快照</span>
          </div>
          {view === 'overview' && data.counts && <Charts data={data} />}
          {view === 'overview' && [2, 3].includes(data.result.report_format_version ?? 1) && (
            <section
              className="audit-highlights"
              aria-label="重点问题与处理建议"
            >
              <h3>重点问题与处理建议</h3>
              {data.result.highlights?.length ? (
                data.result.highlights.map((highlight, index) => {
                  const details = highlightDetails(data, highlight);
                  const open = () => {
                    const p = new URLSearchParams(params);
                    p.set('view', 'rules');
                    p.set('highlight', String(index));
                    p.set('category', 'all');
                    p.set('result', 'all');
                    p.delete('rule');
                    setParams(p, { replace: true });
                  };
                  return (
                    <Card
                      key={`${index}/${highlight.title}`}
                      className="audit-highlight-card"
                      onClick={(event) => {
                        if (
                          !window.getSelection()?.toString() &&
                          !(event.target as HTMLElement).closest(
                            'button,a,input',
                          )
                        )
                          open();
                      }}
                      title={
                        <span>
                          {String(index + 1).padStart(2, '0')} ·{' '}
                          {highlight.title}
                        </span>
                      }
                      extra={<Button onClick={open}>查看关联明细</Button>}
                    >
                      <Space wrap>
                        {details.conclusions.map((value) => (
                          <Tag key={value} color={resultColors[value]}>
                            {resultLabels[value]}
                          </Tag>
                        ))}
                      </Space>
                      <ExpandableContent
                        text={highlight.summary}
                        label="问题概述"
                      >
                        <Markdown text={highlight.summary} />
                      </ExpandableContent>
                      {details.suggestions.length > 0 && (
                        <section className="audit-report-section">
                          <h4>处理建议</h4>
                          {details.suggestions.map((suggestion) => (
                            <div key={suggestion.text}>
                              <div className="audit-muted">
                                {suggestion.ruleIds
                                  .map((id) => labels.get(id))
                                  .join('、')}
                              </div>
                              <ExpandableContent
                                text={suggestion.text}
                                label="处理建议"
                              >
                                <Markdown text={suggestion.text} />
                              </ExpandableContent>
                            </div>
                          ))}
                        </section>
                      )}
                    </Card>
                  );
                })
              ) : (
                <Card>无重点问题，完整结果可在规则明细中查看。</Card>
              )}
            </section>
          )}
          {view === 'rules' && (
            <>
              <div className="audit-toolbar">
                <Space wrap>
                  <Select
                    aria-label="规则类别筛选"
                    value={category}
                    style={{ width: 190 }}
                    onChange={(v) => update('category', v)}
                    options={[
                      { value: 'all', label: '全部类别' },
                      ...Object.entries(categories).map(([value, label]) => ({
                        value,
                        label,
                      })),
                    ]}
                  />
                  <Select
                    aria-label="规则结论筛选"
                    value={conclusion}
                    style={{ width: 160 }}
                    onChange={(v) => update('result', v)}
                    options={[
                      { value: 'issues', label: '问题规则' },
                      { value: 'all', label: '全部规则' },
                      ...Object.entries(resultLabels)
                        .filter(([k]) => k !== 'not_applicable')
                        .map(([value, label]) => ({ value, label })),
                    ]}
                  />
                  <span>{filtered.length} 条规则</span>
                  {activeHighlight && (
                    <Tag
                      closable
                      onClose={() => {
                        const p = new URLSearchParams(params);
                        p.delete('highlight');
                        p.delete('rule');
                        setParams(p, { replace: true });
                      }}
                    >
                      来自重点问题：{activeHighlight.title}
                    </Tag>
                  )}
                </Space>
              </div>
              <div className="audit-report-body">
                <Card
                  title={
                    conclusion === 'issues' ? '问题规则清单' : '规则结论清单'
                  }
                >
                  <Table<Finding>
                    rowKey="rule_id"
                    size="small"
                    dataSource={filtered}
                    rowClassName={(r) =>
                      r.rule_id === chosen?.rule_id ? 'audit-selected-row' : ''
                    }
                    onRow={(r) => ({
                      onClick: () => update('rule', r.rule_id),
                      tabIndex: 0,
                      onKeyDown: (e) => {
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault();
                          update('rule', r.rule_id);
                        }
                      },
                    })}
                    scroll={{ y: 460, x: 430 }}
                    pagination={{ pageSize: 10, showSizeChanger: false }}
                    locale={{
                      emptyText: findings.every((f) => f.result === 'compliant')
                        ? '所有规则均为符合，可切换查看全部规则'
                        : '没有符合筛选条件的规则',
                    }}
                    columns={[
                      {
                        title: '规则 / 核验说明',
                        render: (_, r) => (
                          <>
                            <Button
                              type="link"
                              style={{ padding: 0 }}
                              onClick={() => update('rule', r.rule_id)}
                            >
                              <EntityLabel id={r.rule_id}>
                                {labels.get(r.rule_id) || '规则'}
                              </EntityLabel>
                            </Button>
                            <div className="audit-finding-preview">
                              <Markdown text={r.summary} />
                            </div>
                          </>
                        ),
                      },
                      {
                        title: '重要程度',
                        width: 85,
                        render: (_, r) =>
                          priorities[map.get(r.rule_id)?.priority || ''] || '—',
                      },
                      {
                        title: '结论',
                        width: 98,
                        render: (_, r) => (
                          <Tag color={resultColors[r.result]}>
                            {resultLabels[r.result]}
                          </Tag>
                        ),
                      },
                    ]}
                  />
                </Card>
                <Card
                  title={
                    rule ? (
                      <EntityLabel id={rule.rule_id}>
                        {labels.get(rule.rule_id)} · {categories[rule.category]}
                      </EntityLabel>
                    ) : (
                      '规则详情'
                    )
                  }
                >
                  <div className="audit-rule-inspector" key={chosen?.rule_id}>
                    {rule && chosen ? (
                      <>
                        <Space wrap>
                          <Tag>{priorities[rule.priority]}</Tag>
                          <Tag color="blue">
                            {verifications[rule.verification]}
                          </Tag>
                          <Tag color={resultColors[chosen.result]}>
                            {resultLabels[chosen.result]}
                          </Tag>
                        </Space>
                        <section className="audit-report-section audit-rule-requirement">
                          <h4>规则要求</h4>
                          <ExpandableContent
                            text={rule.requirement}
                            label="规则要求"
                          >
                            <Markdown text={rule.requirement} />
                          </ExpandableContent>
                        </section>
                        {chosen.observation && (
                          <section className="audit-report-section audit-rule-requirement">
                            <h4>实际发现</h4>
                            <ExpandableContent
                              text={chosen.observation}
                              label="实际发现"
                            >
                              <Markdown text={chosen.observation} />
                            </ExpandableContent>
                          </section>
                        )}
                        <section className="audit-report-section audit-finding-explanation">
                          <h4>
                            {[2, 3].includes(data.result.report_format_version ?? 1)
                              ? '判断说明'
                              : '审核说明'}
                          </h4>
                          <ExpandableContent
                            text={chosen.summary}
                            label="审核说明"
                            previewHeight={180}
                          >
                            <Markdown text={chosen.summary} />
                          </ExpandableContent>
                        </section>
                        {chosen.recommendation && (
                          <section className="audit-report-section">
                            <h4>复核建议</h4>
                            <ExpandableContent
                              text={chosen.recommendation}
                              label="复核建议"
                            >
                              <Markdown text={chosen.recommendation} />
                            </ExpandableContent>
                          </section>
                        )}
                        {!!(rule.sources.length || keys.length) && (
                          <section className="audit-report-section audit-report-sources">
                            {!!rule.sources.length && (
                              <>
                                <h4>规则依据文件</h4>
                                <Space wrap>
                                  {rule.sources.map((s) => (
                                    <Button
                                      key={s.file_id}
                                      type="link"
                                      onClick={() => setSource(s.file_id)}
                                    >
                                      <EntityLabel id={s.file_id}>
                                        {fileLabel(s.file_id, rule.sources)}
                                      </EntityLabel>
                                    </Button>
                                  ))}
                                </Space>
                              </>
                            )}
                            {!!keys.length &&
                              keys.map((key) => {
                                const src = data.result?.sources[key];
                                return src ? (
                                  <div key={key}>
                                    <p>
                                      {
                                        (
                                          {
                                            rule_source: '招标',
                                            bid_document: '投标',
                                            evidence_material: '资料',
                                          } as Record<string, string>
                                        )[src.data_role]
                                      }
                                      {src.quote
                                        ? '解析原文摘录'
                                        : chosen.image_paths.includes(key)
                                          ? '图片依据'
                                          : '核验依据'}{' '}
                                      · {src.file_name}
                                      {src.section ? ` · ${src.section}` : ''}
                                      {src.page_numbers.length
                                        ? ` · 页 ${src.page_numbers.join('、')}`
                                        : ''}
                                    </p>
                                    {src.quote && (
                                      <ExpandableContent
                                        text={src.quote}
                                        label="原文摘录"
                                      >
                                        <blockquote>
                                          <Markdown text={src.quote} />
                                        </blockquote>
                                      </ExpandableContent>
                                    )}
                                    <Button
                                      type="link"
                                      onClick={() => setSource(src.file_id)}
                                    >
                                      查看解析原文 · {src.file_name}
                                    </Button>
                                  </div>
                                ) : null;
                              })}
                          </section>
                        )}
                      </>
                    ) : (
                      <Empty description="选择一条规则查看核验内容" />
                    )}
                  </div>
                </Card>
              </div>
            </>
          )}
          {![2, 3].includes(data.result.report_format_version ?? 1) &&
            !!data.result.suggestions.length && (
              <Card className="section-gap" title="历史建议">
                {[...new Set(data.result.suggestions)].map((s, i) => (
                  <p key={s}>
                    {i + 1}. {s}
                  </p>
                ))}
              </Card>
            )}
        </>
      )}
      <SourcePreview
        baseUrl={`/api/audit-tasks${base}`}
        fileId={source}
        sourcePrefix="/sources"
        api={sourceApi}
        download={download}
        close={() => setSource(undefined)}
      />
    </>
  );
}
export default function AuditReport() {
  const { taskId = '' } = useParams();
  const [params, setParams] = useSearchParams();
  const { data, error, reload } = useRemote(
    (s) => api<TaskDetail>(`/${taskId}`, { signal: s }),
    [taskId],
    (d) => d.task.status === 'running',
  );
  const file =
    data?.files.find((f) => f.file_id === params.get('file')) || data?.files[0];
  return (
    <div className="business-page audit-page audit-report-page">
      {!file && (
        <PageHeading
          title="审核报告"
          description={
            data ? (
              <EntityLabel id={taskId}>{data.task.display_name}</EntityLabel>
            ) : undefined
          }
          backTo="/audit-tasks"
          backLabel="返回审核任务"
        />
      )}
      {error && <Failure error={error} retry={reload} />}
      {file ? (
        <ReportFile
          key={`${taskId}/${file.file_id}`}
          taskId={taskId}
          fileId={file.file_id}
          files={data?.files || []}
          taskName={data?.task.display_name || ''}
          onFile={(id) => setParams({ file: id })}
        />
      ) : !data && !error ? (
        <Spin />
      ) : (
        <Empty description="此任务暂无逐文件报告" />
      )}
    </div>
  );
}
