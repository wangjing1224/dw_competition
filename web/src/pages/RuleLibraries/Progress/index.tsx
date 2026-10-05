import { NodeIndexOutlined } from '@ant-design/icons';
import { Link, useParams } from '@umijs/max';
import { Alert, Button, Card, Drawer, Empty, Space, Steps, Tag } from 'antd';
import { useEffect, useRef, useState } from 'react';
import {
  mergeChanges,
  type SeenNode,
  TraceDetail,
  TracePanel,
  TraceTree,
} from '@/components/LibraryTrace';
import {
  api,
  type Detail,
  type NodeDetail,
  type ProgressData,
  phases,
} from '@/services/ruleLibraries';
import { Failure, PageHeading, RuleCard, Status } from '../shared';
import EntityLabel from '@/components/EntityLabel';
import { ruleLabels } from '@/utils/displayLabels';

export { mergeChanges } from '@/components/LibraryTrace';

export default function ProgressPage() {
  const { projectId = '' } = useParams();
  const [data, setData] = useState<ProgressData>();
  const [error, setError] = useState('');
  const [nodes, setNodes] = useState<Record<string, SeenNode>>({});
  const [expandedKeys, setExpandedKeys] = useState<string[]>();
  const [selected, setSelected] = useState<string>();
  const [detail, setDetail] = useState<NodeDetail>();
  const [detailError, setDetailError] = useState('');
  const [reload, setReload] = useState(0);
  const [final, setFinal] = useState<Detail>();
  const cursor = useRef(0);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    cursor.current = 0;
    setNodes({});
    setExpandedKeys(undefined);
    setSelected(undefined);
    setFinal(undefined);
    setData(undefined);
    async function poll() {
      let delay = 2000;
      try {
        if (!document.hidden) {
          const result = await api<ProgressData>(
            `/${projectId}/progress?include_trace=true&after_seq=${cursor.current}&limit=100`,
            { signal: controller.signal },
          );
          setData(result);
          setError('');
          setNodes((old) => mergeChanges(old, result.trace?.changes || []));
          cursor.current = result.trace?.next_cursor ?? cursor.current;
          if (result.trace?.has_more) delay = 0;
          else if (
            result.status !== 'processing_rules' &&
            result.trace?.trace_status !== 'recording'
          ) {
            setFinal(
              await api<Detail>(`/${projectId}`, { signal: controller.signal }),
            );
            return;
          }
        }
      } catch (e) {
        if ((e as Error).name !== 'AbortError') setError((e as Error).message);
        delay = 6000;
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, delay);
    }
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [projectId, reload]);
  const selectedSeq = selected ? nodes[selected]?.seq : undefined;
  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    setDetailError('');
    setDetail(undefined);
    api<NodeDetail>(`/${projectId}/trace/${selected}`, {
      signal: controller.signal,
    })
      .then(setDetail)
      .catch((e) => {
        if (e.name !== 'AbortError') setDetailError(e.message);
      });
    return () => controller.abort();
  }, [projectId, selected, selectedSeq]);
  const phaseOrder = [
    'received',
    'parsing',
    'indexing',
    'extracting',
    'saving',
    'completed',
  ];
  const labels = ruleLabels(
    (data?.rule_order || []).map((rule_id) => ({ rule_id })),
  );
  return (
    <main className="business-page rule-progress-page">
      <PageHeading
        title={
          <EntityLabel id={projectId}>
            {data?.display_name || '规则抽取进度'}
          </EntityLabel>
        }
        description="追踪真实文件处理、模型调用与工具执行；已保存成果可随时查看。"
        actions={
          <>
            <Status status={data?.status || 'processing_rules'} />
            <Link to={`/rule-libraries/${projectId}`}>
              <Button>查看当前规则</Button>
            </Link>
          </>
        }
      />
      {error && <Failure error={error} retry={() => setReload((n) => n + 1)} />}
      {data?.error && (
        <Alert
          className="section-gap"
          type="error"
          showIcon
          title={data.error.message}
          description={`失败阶段：${phases[data.failed_phase || ''] || '未知'}。已保存的规则和原文件保留，不自动重跑。`}
        />
      )}
      <Card className="section-gap">
        <Steps
          size="small"
          current={Math.max(
            0,
            phaseOrder.indexOf(
              data?.phase === 'failed'
                ? data.failed_phase || ''
                : data?.phase || 'received',
            ),
          )}
          status={data?.status === 'rule_failed' ? 'error' : undefined}
          items={phaseOrder.map((key) => ({ title: phases[key] }))}
        />
        <div className="progress-summary">
          <span>
            已完成文件{' '}
            <strong>
              {data?.completed_file_ids.length || 0} /{' '}
              {data?.document_count || 0}
            </strong>
          </span>
          <span>
            已保存规则 <strong>{data?.saved_rule_count || 0}</strong>
          </span>
          <span>
            {data?.status === 'processing_rules'
              ? '后台处理中，可离开页面后再返回'
              : '处理已结束'}
          </span>
        </div>
      </Card>
      {final && (
        <Alert
          className="section-gap"
          type={
            data?.status === 'rule_failed' || final.warning
              ? 'warning'
              : 'success'
          }
          title={
            data?.status === 'rule_failed'
              ? '处理失败，可查看已保存的阶段结果和来源文件'
              : final.warning?.code === 'rule_set_unavailable'
                ? '规则快照不可读取，仍可查看来源文件'
                : final.rule_set?.rules.length === 0
                  ? '处理完成，但未提取到规则'
                  : '处理结果已保存'
          }
          action={
            <Link to={`/rule-libraries/${projectId}`}>
              <Button type="primary">进入规则审阅</Button>
            </Link>
          }
        />
      )}
      <div className="trace-grid">
        <TracePanel
          title={`来源文件 · ${data?.document_count || 0}`}
          label="来源文件列表"
        >
          <div className="file-progress-list">
            {data?.files.map((file) => (
              <div
                key={file.file_id}
                className={`file-progress ${file.extraction_status === 'processing' ? 'active' : ''}`}
              >
                <strong>
                  <EntityLabel id={file.file_id}>{file.file_name}</EntityLabel>
                </strong>
                <small>{file.source_path}</small>
                <Space wrap>
                  <Tag
                    color={
                      file.extraction_status === 'completed'
                        ? 'green'
                        : file.extraction_status === 'failed'
                          ? 'red'
                          : 'blue'
                    }
                  >
                    {
                      {
                        pending: '等待抽取',
                        processing: '抽取中',
                        completed: '已完成',
                        failed: '失败',
                      }[file.extraction_status || 'pending']
                    }
                  </Tag>
                </Space>
                <small>
                  解析：
                  {(
                    {
                      pending: '等待解析',
                      parsed: '完成',
                      partial: '部分可读',
                      ocr_required: '需识图',
                      manual_review: '需人工处理',
                      unsupported: '不支持',
                    } as Record<string, string>
                  )[file.parse_status] || file.parse_status}
                </small>
              </div>
            ))}
          </div>
        </TracePanel>
        <TracePanel
          label="Agent 执行轨迹内容"
          title={
            <Space>
              <NodeIndexOutlined />
              Agent 执行轨迹
            </Space>
          }
          extra={<span className="muted">点击节点查看详情</span>}
        >
          {data?.trace?.warning && (
            <Alert type="warning" title={data.trace.warning} />
          )}
          {Object.keys(nodes).length ? (
            <TraceTree
              nodes={nodes}
              expandedKeys={expandedKeys}
              onExpand={setExpandedKeys}
              onSelect={setSelected}
            />
          ) : (
            <Empty description="尚无 Agent 调用记录；解析和建索引完成后显示。" />
          )}
        </TracePanel>
        <TracePanel title="最近保存的规则" label="最近保存的规则内容">
          <div className="recent-rules">
            {data?.recent_rules.length ? (
              data.recent_rules.map((rule) => (
                <RuleCard
                  key={rule.rule_id}
                  rule={rule}
                  label={labels.get(rule.rule_id)}
                />
              ))
            ) : (
              <Empty description="暂无已保存规则" />
            )}
          </div>
        </TracePanel>
      </div>
      <Drawer
        title={detail?.node.title || '执行详情'}
        open={!!selected}
        onClose={() => setSelected(undefined)}
        size="large"
      >
        {detailError && <Failure error={detailError} />}
        {!detail && !detailError && <p>读取中…</p>}
        {detail && <TraceDetail detail={detail} />}
      </Drawer>
    </main>
  );
}
