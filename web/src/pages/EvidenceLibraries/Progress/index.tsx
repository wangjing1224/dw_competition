import { Link, useParams } from '@umijs/max';
import { Alert, Button, Card, Drawer, Empty, Space, Steps, Tag } from 'antd';
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  mergeChanges,
  type SeenNode,
  TracePanel,
  TraceTree,
} from '@/components/LibraryTrace';
import SourcePreview from '@/components/SourcePreview';
import {
  api,
  download,
  type NodeDetail,
  type ProgressData,
  phases,
  processing,
} from '@/services/evidenceLibraries';
import { Failure, Heading, Status } from '../shared';

export default function Progress() {
  const { bidderId = '' } = useParams();
  const [data, setData] = useState<ProgressData>();
  const [nodes, setNodes] = useState<Record<string, SeenNode>>({});
  const [expanded, setExpanded] = useState<string[]>();
  const [selected, setSelected] = useState<string>();
  const [detail, setDetail] = useState<NodeDetail>();
  const [error, setError] = useState('');
  const [detailError, setDetailError] = useState('');
  const [reload, setReload] = useState(0);
  const [previewFile, setPreviewFile] = useState<string>();
  const read = useCallback(
    <T,>(path: string, init?: RequestInit) =>
      api<T>(`/${bidderId}${path}`, init),
    [bidderId],
  );
  const saveFile = useCallback(
    (path: string, filename: string) =>
      download(`/${bidderId}${path}`, filename),
    [bidderId],
  );
  const cursor = useRef(0);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    cursor.current = 0;
    setData(undefined);
    setNodes({});
    setExpanded(undefined);
    setSelected(undefined);
    async function poll() {
      let delay = 2000;
      try {
        if (!document.hidden) {
          const result = await api<ProgressData>(
            `/${bidderId}/progress?include_trace=true&after_seq=${cursor.current}&limit=100`,
            { signal: controller.signal },
          );
          if (controller.signal.aborted) return;
          setData(result);
          setError('');
          setNodes((old) => mergeChanges(old, result.trace?.changes || []));
          cursor.current = result.trace?.next_cursor ?? cursor.current;
          if (result.trace?.has_more) delay = 0;
          else if (
            !processing(result.status) &&
            result.trace?.trace_status !== 'recording'
          )
            return;
        }
      } catch (e) {
        if (!controller.signal.aborted) setError((e as Error).message);
        delay = 6000;
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, delay);
    }
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [bidderId, reload]);
  const seq = selected ? nodes[selected]?.seq : undefined;
  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    setDetail(undefined);
    setDetailError('');
    api<NodeDetail>(`/${bidderId}/trace/${selected}`, {
      signal: controller.signal,
    })
      .then(setDetail)
      .catch((e) => {
        if (!controller.signal.aborted) setDetailError(e.message);
      });
    return () => controller.abort();
  }, [bidderId, selected, seq]);
  const order = ['received', 'parsing', 'indexing', 'ingesting', 'completed'];
  const phase = data?.phase === 'failed' ? data.failed_phase : data?.phase;
  return (
    <main className="business-page rule-progress-page">
      <Heading
        title={data?.display_name || '资料整理进度'}
        description="查看实际文件处理与 Agent 执行情况，已保存摘要可随时查阅。"
        actions={
          <>
            <Status status={data?.status || 'processing_evidence'} />
            <Link to={`/evidence-libraries/${bidderId}`}>
              <Button>查看当前资料</Button>
            </Link>
          </>
        }
      />
      {error && <Failure error={error} retry={() => setReload((n) => n + 1)} />}
      {data?.error && (
        <Alert
          className="section-gap"
          type="error"
          title={data.error.message}
          description={`失败阶段：${phases[data.failed_phase || ''] || '未知'}；已保存资料与原件保留，不自动重跑。`}
        />
      )}
      {data?.warning && (
        <Alert
          className="section-gap"
          type="warning"
          title={data.warning.message}
        />
      )}
      <Card className="section-gap">
        <Steps
          size="small"
          current={Math.max(0, order.indexOf(phase || 'received'))}
          status={data?.phase === 'failed' ? 'error' : undefined}
          items={order.map((key) => ({ title: phases[key] }))}
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
            已保存摘要 <strong>{data?.saved_material_count ?? '—'}</strong>
          </span>
          <span>
            {data && !processing(data.status)
              ? '处理已结束'
              : '后台处理中，可离开页面后再返回'}
          </span>
        </div>
      </Card>
      {data?.status === 'evidence_ready' && (
        <Alert
          className="section-gap"
          type="success"
          title="处理结果已保存"
          action={
            <Link to={`/evidence-libraries/${bidderId}`}>
              <Button type="primary">进入资料审阅</Button>
            </Link>
          }
        />
      )}
      <div className="trace-grid">
        <TracePanel
          title={`来源文件 · ${data?.document_count || 0}`}
          label="资料文件列表"
        >
          {data?.files.map((file) => (
            <div
              key={file.file_id}
              className={`file-progress ${file.ingestion_status === 'processing' ? 'active' : ''}`}
            >
              <Button type="link" onClick={() => setPreviewFile(file.file_id)}>
                {file.file_name}
              </Button>
              <small>{file.source_path}</small>
              <Space wrap>
                <code>{file.file_id}</code>
                <Tag
                  color={
                    file.ingestion_status === 'completed'
                      ? 'green'
                      : file.ingestion_status === 'failed'
                        ? 'red'
                        : 'blue'
                  }
                >
                  {
                    {
                      pending: '等待整理',
                      processing: '整理中',
                      completed: '已完成',
                      failed: '失败',
                    }[file.ingestion_status]
                  }
                </Tag>
              </Space>
            </div>
          ))}
        </TracePanel>
        <TracePanel
          title="Agent 执行轨迹"
          label="资料 Agent 执行轨迹"
          extra={<span className="muted">点击节点查看详情</span>}
        >
          {data?.trace?.warning && (
            <Alert type="warning" title={data.trace.warning} />
          )}
          {Object.keys(nodes).length ? (
            <TraceTree
              nodes={nodes}
              expandedKeys={expanded}
              onExpand={setExpanded}
              onSelect={setSelected}
            />
          ) : (
            <Empty description="尚无 Agent 调用记录；解析和建索引完成后显示。" />
          )}
        </TracePanel>
        <TracePanel title="最近保存的摘要" label="最近保存的资料摘要">
          {data?.recent_materials.length ? (
            data.recent_materials.map((material) => (
              <Card
                key={material.file_id}
                className="section-gap"
                size="small"
                title={material.file_id}
              >
                <p className="material-text">{material.bid_summary}</p>
                <Link
                  to={`/evidence-libraries/${bidderId}?file=${material.file_id}`}
                >
                  查看资料
                </Link>
              </Card>
            ))
          ) : (
            <Empty description="暂无已保存摘要" />
          )}
        </TracePanel>
      </div>
      <SourcePreview
        baseUrl={`/api/bidders/${bidderId}`}
        fileId={previewFile}
        close={() => setPreviewFile(undefined)}
        api={read}
        download={saveFile}
      />
      <Drawer
        title={detail?.node.title || '执行详情'}
        open={!!selected}
        onClose={() => setSelected(undefined)}
        size="large"
      >
        {detailError && <Failure error={detailError} />}
        {!detail && !detailError && <p>读取中…</p>}
        {detail && (
          <>
            {detail.truncated && (
              <Alert type="warning" title="长内容仅展示前部，真实调用未改变" />
            )}
            {detail.error && (
              <Alert type="error" title={detail.error.message} />
            )}
            <h3>输入</h3>
            <pre className="trace-content">
              {JSON.stringify(detail.input, null, 2) || '无'}
            </pre>
            <h3>输出</h3>
            <pre className="trace-content">
              {typeof detail.output === 'string'
                ? detail.output
                : JSON.stringify(detail.output, null, 2) || '等待返回'}
            </pre>
            {detail.material_change && (
              <>
                <h3>已保存变更（修改前 → 修改后）</h3>
                <pre className="trace-content">
                  {JSON.stringify(detail.material_change, null, 2)}
                </pre>
              </>
            )}
          </>
        )}
      </Drawer>
    </main>
  );
}
