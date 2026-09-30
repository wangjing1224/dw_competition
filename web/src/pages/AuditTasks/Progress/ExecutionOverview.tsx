import { Link } from '@umijs/max';
import { Button } from 'antd';
import type { AuditFile, ProgressData } from '@/services/auditTasks';
import { fileUrl } from '@/services/auditTasks';
import { Status } from '../shared';
import {
  overviewText,
  phases,
  skippedAudit,
  stage,
  stageText,
} from './graphModel';

export default function ExecutionOverview({
  task,
  file,
  activity,
  onStage,
  onDraft,
}: {
  task: string;
  file: AuditFile;
  activity: ProgressData['activity'];
  onStage: (phase: string) => void;
  onDraft: () => void;
}) {
  const counts = skippedAudit(file)
    ? [['model_calls', '模型调用']]
    : [
        ['model_calls', '模型调用'],
        ['tool_calls', '工具调用'],
        ['evidence_checks', '资料核验'],
        ['report_saves', '报告保存'],
      ];
  return (
    <section className="audit-panel audit-overview" aria-label="当前文件进度">
      <div className="audit-overview-heading">
        <strong>当前文件进度</strong>
        <Status value={file.status} />
        <span className="audit-overview-summary" title={overviewText(file)}>
          {overviewText(file)}
        </span>
        {file.report_kind === 'final' ? (
          <Link to={fileUrl(task, file.file_id)}>
            <Button type="primary">查看报告</Button>
          </Link>
        ) : file.report_kind === 'draft' ? (
          <Button onClick={onDraft}>查看草稿</Button>
        ) : (
          <span className="audit-muted">报告尚未生成</span>
        )}
      </div>
      <div className="audit-stages">
        {phases.map(([phase, label]) => {
          const value = stage(file, phase).status;
          return (
            <button
              type="button"
              key={phase}
              onClick={() => onStage(phase)}
              className={`stage-${value}`}
            >
              <span className="audit-stage-icon">
                {value === 'completed'
                  ? '✓'
                  : value === 'running'
                    ? '↻'
                    : value === 'failed'
                      ? '!'
                      : '−'}
              </span>
              <span>
                <strong>{label}</strong>
                <small>{stageText(file, phase)}</small>
              </span>
            </button>
          );
        })}
      </div>
      <div className="audit-observed">
        <span>当前文件 · 已观测</span>
        {counts.map(([key, label]) => (
          <span
            key={key}
            title={
              key === 'report_saves'
                ? 'Agent 报告工具成功保存事件数，不代表正式报告份数'
                : undefined
            }
          >
            {label}
            <strong>{activity[key as 'model_calls'] ?? '—'}</strong>
            {key === 'evidence_checks' ? '项' : '次'}
          </span>
        ))}
        {skippedAudit(file) && <span>未启动审核 Agent</span>}
        {activity.observation !== 'complete' && (
          <span className="audit-observation-warning">
            {activity.observation === 'unavailable'
              ? '观测不可用'
              : '观测不完整'}
          </span>
        )}
      </div>
    </section>
  );
}
