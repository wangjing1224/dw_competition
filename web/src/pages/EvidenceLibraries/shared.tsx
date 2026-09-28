import { Tag } from 'antd';
import type { ComponentProps } from 'react';
import { PageHeading } from '../RuleLibraries/shared';

export { Failure } from '../RuleLibraries/shared';

export function Heading(props: ComponentProps<typeof PageHeading>) {
  return (
    <PageHeading
      backTo="/evidence-libraries"
      backLabel="返回资料库"
      {...props}
    />
  );
}
export function Status({ status }: { status: string }) {
  return (
    <Tag
      color={
        status === 'evidence_ready'
          ? 'green'
          : status === 'evidence_failed'
            ? 'red'
            : 'blue'
      }
    >
      {{
        uploading_evidence: '接收中',
        processing_evidence: '处理中',
        evidence_ready: '已完成',
        evidence_failed: '失败',
      }[status] || status}
    </Tag>
  );
}
export function Provenance({ value }: { value: string | null }) {
  return value ? (
    <Tag color={value === 'human_modified' ? 'orange' : 'blue'}>
      {value === 'human_modified' ? '人工修订' : 'AI 整理'}
    </Tag>
  ) : null;
}
