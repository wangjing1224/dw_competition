// biome-ignore-all lint/security/noDangerouslySetInnerHtml: HTML 只来自经过测试的 safeMarkdown 净化器。
import { Alert, App, Button, Empty, Space, Tag } from 'antd';
import EntityLabel from '@/components/EntityLabel';
import { useEffect, useState } from 'react';
import type { DependencyList } from 'react';
import { safeMarkdown } from '@/components/SourcePreview';
import {
  downloadFile,
  statusLabels,
  stageStates,
  stageLabels,
} from '@/services/auditTasks';
import type { AuditFile } from '@/services/auditTasks';
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
export function FileErrors({ file }: { file: AuditFile }) {
  return (
    <>
      {file.error && <Alert type="error" title={file.error.message} />}
      {file.export_error && (
        <Alert
          type="warning"
          title={`Word 导出失败：${file.export_error.message}`}
          description="已保存的正式结果与 Markdown 不受影响。"
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
      <table className="audit-matrix">
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
            >
              <td>
                <button type="button" onClick={() => onSelect(f)}>
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
