import { Link, useParams, useSearchParams } from '@umijs/max';
import EntityLabel from '@/components/EntityLabel';
import { fileLabel, ruleLabels } from '@/utils/displayLabels';
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
import SourcePreview from '@/components/SourcePreview';
import { PageHeading, Failure } from '@/pages/RuleLibraries/shared';
import {
  categories,
  priorities,
  verifications,
} from '@/services/ruleLibraries';
import {
  api,
  fileUrl,
  orderedFindings,
  resultColors,
  resultLabels,
  downloadFile,
} from '@/services/auditTasks';
import type { Finding, ReportData, TaskDetail } from '@/services/auditTasks';
import {
  useRemote,
  Status,
  Download,
  Binding,
  FileErrors,
  Markdown,
} from '../shared';

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
      <div>
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
        <div className="audit-legend">
          {Object.keys(resultColors).map((k) => (
            <span key={k}>
              <i style={{ background: resultColors[k] }} />
              {resultLabels[k]} {counts[k]}
            </span>
          ))}
        </div>
      </div>
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
      <div>
        <span className="audit-muted">当前文件总体结论</span>
        <p className="audit-conclusion">
          {resultLabels[result.overall_conclusion] || result.overall_conclusion}
        </p>
        <p>{result.audit_summary || '详见逐条审核说明。'}</p>
        <span className="audit-muted">
          统计完整正式结果，不随下方筛选改变；问题数为规则条数，非独立缺陷数。
        </span>
      </div>
    </section>
  );
}
function ReportFile({ taskId, fileId }: { taskId: string; fileId: string }) {
  const [params, setParams] = useSearchParams();
  const view = params.get('view') === 'rules' ? 'rules' : 'overview';
  const category = params.get('category') || 'all';
  const conclusion = params.get('result') || 'issues';
  const selected = params.get('rule');
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
  const filtered = findings.filter(
    (f) =>
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
  if (error && !data) return <Failure error={error} retry={reload} />;
  if (!data) return <Spin />;
  return (
    <>
      {error && <Failure error={error} retry={reload} />}
      <div className="audit-toolbar">
        <Binding file={data.file} />
        <Space wrap>
          <Button onClick={() => setSource(fileId)}>查看解析原文</Button>
          <Download
            url={data.file.outputs.word_url}
            name={`${data.file.file_name}-审核报告.docx`}
            label="下载 Word"
          />
          <Download
            url={data.file.outputs.md_url}
            name={`${fileId}-审核报告.md`}
            label="导出 Markdown"
          />
          <Download
            url={data.file.outputs.json_url}
            name={`${fileId}-审核报告.json`}
            label="导出 JSON"
          />
        </Space>
      </div>
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
      ) : !data.result?.is_applicable ? (
        <Card title="项目适用性判断">
          <Tag color="orange">
            {resultLabels[data.result?.overall_conclusion || ''] ||
              '未进入自动审核'}
          </Tag>
          <p>
            {data.result?.exclusion_reason || data.result?.project_match.reason}
          </p>
          <Alert
            type="info"
            title="此文件未进入规则审核，不生成规则结论统计图。"
          />
        </Card>
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
          <div className="audit-toolbar">
            <Space wrap>
              <Select
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
                value={conclusion}
                style={{ width: 160 }}
                onChange={(v) => update('result', v)}
                options={[
                  { value: 'issues', label: '问题规则' },
                  { value: 'all', label: '全部结论' },
                  ...Object.entries(resultLabels)
                    .filter(([k]) => k !== 'not_applicable')
                    .map(([value, label]) => ({ value, label })),
                ]}
              />
              <span>{filtered.length} 条规则</span>
            </Space>
            <Button onClick={() => update('result', 'all')}>
              查看全部规则
            </Button>
          </div>
          <div className="audit-report-body">
            <Card
              title={conclusion === 'issues' ? '问题规则清单' : '规则结论清单'}
            >
              <Table<Finding>
                rowKey="rule_id"
                size="small"
                dataSource={filtered}
                rowClassName={(r) =>
                  r.rule_id === chosen?.rule_id ? 'audit-selected-row' : ''
                }
                onRow={(r) => ({ onClick: () => update('rule', r.rule_id) })}
                scroll={{ y: 520, x: 430 }}
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
                        <div style={{ maxWidth: 380, whiteSpace: 'normal' }}>
                          {r.summary}
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
              <div className="audit-rule-inspector">
                {rule && chosen ? (
                  <>
                    <Space wrap>
                      <Tag>{priorities[rule.priority]}</Tag>
                      <Tag color="blue">{verifications[rule.verification]}</Tag>
                      <Tag color={resultColors[chosen.result]}>
                        {resultLabels[chosen.result]}
                      </Tag>
                    </Space>
                    <h4>规则要求</h4>
                    <p>{rule.requirement}</p>
                    <h4>依据说明</h4>
                    <p>{rule.reason || '未补充依据说明'}</p>
                    <h4>审核说明</h4>
                    <p>{chosen.summary}</p>
                    <h4>招标来源</h4>
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
                    <h4>已记录核验来源</h4>
                    {keys.length ? (
                      keys.map((key) => {
                        const src = data.result?.sources[key];
                        return src ? (
                          <div key={key}>
                            <Tag>{key}</Tag>
                            <p>
                              {src.section || '文件级来源'}
                              {src.page_numbers.length
                                ? ` · 页 ${src.page_numbers.join('、')}`
                                : ''}
                            </p>
                            {src.quote && <blockquote>{src.quote}</blockquote>}
                            <Button
                              type="link"
                              onClick={() => setSource(src.file_id)}
                            >
                              查看解析原文 · {src.file_name}
                            </Button>
                          </div>
                        ) : (
                          <p key={key}>{key} · 未登记可查看来源</p>
                        );
                      })
                    ) : (
                      <p className="audit-muted">
                        此项未记录引用；不从审核说明推测原文位置。
                      </p>
                    )}
                  </>
                ) : (
                  <Empty description="选择一条规则查看核验内容" />
                )}
              </div>
            </Card>
          </div>
          {view === 'rules' && (
            <Card className="section-gap" title="完整审核报告">
              <Markdown text={data.document.markdown || ''} />
            </Card>
          )}
          {!!data.result.suggestions.length && (
            <Card className="section-gap" title="后续处理建议">
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
    <div className="business-page audit-page">
      <PageHeading
        title={
          file ? (
            <EntityLabel id={file.file_id}>{file.file_name}</EntityLabel>
          ) : (
            '审核报告'
          )
        }
        description={
          data ? (
            <EntityLabel id={taskId}>{data.task.display_name}</EntityLabel>
          ) : undefined
        }
        backTo="/audit-tasks"
        backLabel="返回审核任务"
        actions={
          <Space>
            {file && <Status value={file.status} />}
            {file && (
              <Link to={fileUrl(taskId, file.file_id, 'progress')}>
                <Button>查看处理过程</Button>
              </Link>
            )}
          </Space>
        }
      />
      {error && <Failure error={error} retry={reload} />}
      <div className="audit-toolbar">
        <Select
          aria-label="切换报告文件"
          className="audit-file-selector"
          value={file?.file_id}
          onChange={(v) => setParams({ file: v })}
          options={data?.files.map((f) => ({
            value: f.file_id,
            label: fileLabel(f.file_id, data.files),
          }))}
        />
      </div>
      {file ? (
        <ReportFile
          key={`${taskId}/${file.file_id}`}
          taskId={taskId}
          fileId={file.file_id}
        />
      ) : !data && !error ? (
        <Spin />
      ) : (
        <Empty description="此任务暂无逐文件报告" />
      )}
    </div>
  );
}
