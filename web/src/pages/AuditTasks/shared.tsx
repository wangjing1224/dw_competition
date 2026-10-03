// biome-ignore-all lint/security/noDangerouslySetInnerHtml: HTML 只来自经过测试的 safeMarkdown 净化器。
import { Alert, App, Button, Empty, Space, Tag } from 'antd';
import type { DependencyList } from 'react';
import { useEffect, useState } from 'react';
import EntityLabel from '@/components/EntityLabel';
import { safeMarkdown } from '@/components/SourcePreview';
import type { AuditFile, Problem } from '@/services/auditTasks';
import {
  downloadFile,
  stageLabels,
  stageStates,
  statusLabels,
} from '@/services/auditTasks';
import './audit.less';

// 每次身份变化丢弃旧数据；轮询不重叠，晚到响应不覆盖其他文件。
export function useRemote<T>(
  load: (signal: AbortSignal) => Promise<T>,
  deps: DependencyList,
  poll?: (data: T) => boolean,
) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    setData(undefined);
    setError('');
    const tick = async () => {
      try {
        const value = await load(controller.signal);
        if (controller.signal.aborted) return;
        setData(value);
        setError('');
        if (poll?.(value)) timer = setTimeout(tick, 2000);
      } catch (e) {
        if (!controller.signal.aborted) {
          setError((e as Error).message);
          if (poll) timer = setTimeout(tick, 4000);
        }
      }
    };
    void tick();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
    // Identity dependencies are explicitly provided by each caller.
  }, [...deps, retry]);
  return { data, error, reload: () => setRetry((v) => v + 1) };
}
export function Status({ value }: { value: string }) {
  return (
    <Tag
      color={
        value === 'completed'
          ? 'green'
          : value.includes('fail') || value.includes('error')
            ? 'red'
            : value === 'running'
              ? 'blue'
              : 'default'
      }
    >
      {statusLabels[value] || stageStates[value] || value}
    </Tag>
  );
}
export function Download({
  url,
  label,
  name,
}: {
  url?: string | null;
  label: string;
  name: string;
}) {
  const { message } = App.useApp();
  return (
    <Button
      disabled={!url}
      onClick={() =>
        url && downloadFile(url, name).catch((e) => message.error(e.message))
      }
    >
      {label}
    </Button>
  );
}
function fileFailureText(file: AuditFile) {
  // 兼容已保存的错误文本，只概括明确的缺项，不推测是否耗尽轮次。
  const missing = file.error?.message.match(
    /(?:^|[:：]\s*)报告未完成[:：]\s*([\s\S]*)$/u,
  )?.[1];
  if (missing !== undefined) {
    const count = new Set(
      missing.split(/[、,，\s]+/u).filter((item) => /^rule-\d+$/u.test(item)),
    ).size;
    return {
      title: '审核报告未完成',
      description: count
        ? `还有 ${count} 条规则未形成完整结论，未生成正式报告。`
        : '报告内容尚未补齐，未生成正式报告。',
    };
  }
  const phase =
    file.stages.find((s) => s.status === 'failed')?.phase || file.phase;
  return {
    title: `${stageLabels[phase] || '文件处理'}未完成`,
    description: '处理过程中出现异常，可展开技术详情查看具体原因。',
  };
}
function ErrorDetails({ error }: { error: NonNullable<Problem> }) {
  return (
    <details className="audit-error-details">
      <summary>技术详情</summary>
      <pre>{`错误代码：${error.code}\n${error.message}`}</pre>
    </details>
  );
}
export function FileErrors({ file }: { file: AuditFile }) {
  const summary = file.error ? fileFailureText(file) : null;
  return (
    <>
      {file.error && summary && (
        <Alert
          showIcon
          type="error"
          title={summary.title}
          description={
            <>
              <div>
                {summary.description}
                {file.report_kind === 'draft' && '已保存草稿可供查看。'}
              </div>
              <ErrorDetails
                key={`${file.file_id}/${file.error.code}/${file.error.message}`}
                error={file.error}
              />
            </>
          }
        />
      )}
      {file.export_error && (
        <Alert
          showIcon
          type="warning"
          title={`${file.export_error.code === 'markdown_export_failed' ? 'Markdown' : 'Word'} 导出失败`}
          description={
            <>
              <div>
                {file.export_error.code === 'markdown_export_failed'
                  ? '正式 JSON 已保存；Markdown 未生成，Word 尚未导出。'
                  : '已保存的正式结果与 Markdown 不受影响。'}
              </div>
              <ErrorDetails
                key={`${file.file_id}/${file.export_error.code}/${file.export_error.message}`}
                error={file.export_error}
              />
            </>
          }
        />
      )}
    </>
  );
}
export function StageMatrix({
  files,
  onSelect,
  selected,
}: {
  files: AuditFile[];
  onSelect: (f: AuditFile) => void;
  selected?: string;
}) {
  return files.length ? (
    <div className="audit-matrix-scroll">
      <table className="audit-matrix" aria-label="文件处理矩阵">
        <thead>
          <tr>
            <th>投标文件</th>
            {Object.values(stageLabels).map((s) => (
              <th key={s}>{s}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {files.map((f) => (
            <tr
              key={f.file_id}
              className={selected === f.file_id ? 'is-selected' : ''}
              aria-selected={selected === f.file_id}
              onClick={() => onSelect(f)}
            >
              <td>
                {/* 名称按钮保留原生键盘操作，点击统一冒泡到行，避免重复选择。 */}
                <button type="button" aria-pressed={selected === f.file_id}>
                  <EntityLabel id={f.file_id}>{f.file_name}</EntityLabel>
                </button>
                <small>{f.source_path}</small>
              </td>
              {Object.keys(stageLabels).map((phase) => {
                const stage = f.stages.find((s) => s.phase === phase);
                return (
                  <td key={phase}>
                    <span
                      className={`stage-${stage?.status || 'unknown'}`}
                      title={stage?.reason || ''}
                    >
                      {stageStates[stage?.status || ''] || '未记录'}
                    </span>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  ) : (
    <Empty description="此任务暂无逐文件记录" />
  );
}
export function Markdown({ text }: { text: string }) {
  // 与原文预览共用净化器，报告不自动加载外部图片或跳转外部链接。
  return (
    <article
      className="document-preview audit-markdown"
      dangerouslySetInnerHTML={{ __html: safeMarkdown(text, []) }}
    />
  );
}
export function Binding({ file }: { file: AuditFile }) {
  return (
    <Space wrap>
      <Tag color="blue">
        <EntityLabel id={file.project_id}>
          规则库 · {file.project_name}
        </EntityLabel>
      </Tag>
      <Tag color="purple">
        <EntityLabel id={file.bidder_id}>
          资料库 · {file.bidder_name}
        </EntityLabel>
      </Tag>
    </Space>
  );
}
