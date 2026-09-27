import { Alert, Button, Empty, Space, Tag } from 'antd';
import { Link } from '@umijs/max';
import { EditOutlined, FileTextOutlined } from '@ant-design/icons';
import type { ReactNode } from 'react';
import {
  priorities,
  provenances,
  statuses,
  verifications,
  type Rule,
} from '@/services/ruleLibraries';

export function PageHeading({
  title,
  description,
  actions,
  back = true,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
  back?: boolean;
}) {
  return (
    <header className="page-heading">
      <div>
        {back && (
          <Link className="back-link" to="/rule-libraries">
            规则库 /
          </Link>
        )}
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      <Space wrap>{actions}</Space>
    </header>
  );
}
export function Status({ status }: { status: string }) {
  return (
    <Tag
      color={
        status === 'rule_ready'
          ? 'green'
          : status === 'rule_failed'
            ? 'red'
            : 'blue'
      }
    >
      {statuses[status] || status}
    </Tag>
  );
}
export function Failure({
  error,
  retry,
}: {
  error: string;
  retry?: () => void;
}) {
  return (
    <Alert
      type="error"
      showIcon
      title={error}
      action={retry && <Button onClick={retry}>重试读取</Button>}
    />
  );
}
export function NoRules() {
  return (
    <Empty description="当前没有规则。文件可能只含说明性内容，也可在处理完成后人工补充。" />
  );
}
export function RuleCard({
  rule,
  onSource,
  onEdit,
}: {
  rule: Rule;
  onSource?: (id: string) => void;
  onEdit?: () => void;
}) {
  return (
    <article className="rule-card">
      <div className="rule-card-top">
        <Space wrap>
          <code>{rule.rule_id}</code>
          <Tag
            color={
              rule.priority === 'critical'
                ? 'red'
                : rule.priority === 'important'
                  ? 'orange'
                  : 'default'
            }
          >
            {priorities[rule.priority]}
          </Tag>
          <Tag color="blue">{verifications[rule.verification]}</Tag>
          <span className="muted">{provenances[rule.provenance]}</span>
        </Space>
        {onEdit && (
          <Button type="text" icon={<EditOutlined />} onClick={onEdit}>
            编辑
          </Button>
        )}
      </div>
      <p className="requirement">{rule.requirement}</p>
      {rule.reason && (
        <div className="rule-reason">
          <strong>依据说明</strong>
          <p>{rule.reason}</p>
        </div>
      )}
      <div className="rule-sources">
        <FileTextOutlined />
        <span>来源</span>
        {rule.sources.map((source) => (
          <Button
            size="small"
            type="link"
            key={source.file_id}
            onClick={() => onSource?.(source.file_id)}
            disabled={!onSource}
          >
            {source.file_name}
          </Button>
        ))}
      </div>
    </article>
  );
}
