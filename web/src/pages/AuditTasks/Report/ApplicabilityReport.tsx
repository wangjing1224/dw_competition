import {
  CheckCircleFilled,
  DownOutlined,
  ExclamationCircleFilled,
} from '@ant-design/icons';
import { Alert, App, Button, Card, Dropdown, Space, Tag } from 'antd';
import EntityLabel from '@/components/EntityLabel';
import type { ReportData } from '@/services/auditTasks';
import { downloadFile } from '@/services/auditTasks';
import { skippedAudit, stage, statusText } from '../Progress/graphModel';
import './applicability.less';

export function ExplanationDownloads({ data }: { data: ReportData }) {
  const { message } = App.useApp();
  const file = data.file;
  const ready = file.word_status === 'ready' && !!file.outputs.word_url;
  const download = (format: 'word' | 'md' | 'json') => {
    const url = file.outputs[`${format}_url`];
    if (url)
      void downloadFile(
        url,
        `${file.file_name}-项目适用性说明.${format === 'word' ? 'docx' : format}`,
      ).catch((e) => message.error(e.message));
  };
  return (
    <Space>
      <Button type="primary" disabled={!ready} onClick={() => download('word')}>
        {ready
          ? '下载 Word'
          : file.word_status === 'failed'
            ? 'Word 导出失败'
            : file.word_status === 'generating'
              ? 'Word 生成中'
              : 'Word 未就绪'}
      </Button>
      <Dropdown
        trigger={['click']}
        menu={{
          items: [
            {
              key: 'md',
              label: '导出 Markdown',
              disabled: !file.outputs.md_url,
            },
            {
              key: 'json',
              label: '导出 JSON',
              disabled: !file.outputs.json_url,
            },
          ],
          onClick: ({ key }) => download(key as 'md' | 'json'),
        }}
      >
        <Button>
          更多导出 <DownOutlined />
        </Button>
      </Dropdown>
    </Space>
  );
}
export default function ApplicabilityReport({
  data,
  uncertain,
}: {
  data: ReportData;
  uncertain: boolean;
}) {
  const file = data.file;
  const reason =
    data.result?.exclusion_reason?.trim() ||
    data.result?.project_match.reason?.trim() ||
    file.route?.match_reason?.trim() ||
    '未记录判断原因';
  const parsed = stage(file, 'parsing'),
    routed = stage(file, 'routing'),
    auditing = stage(file, 'auditing');
  return (
    <>
      <section className="audit-applicability-result">
        <div className="audit-applicability-message">
          <ExclamationCircleFilled />
          <div>
            <Tag color="orange">
              {uncertain ? '适用性待判断' : '项目不匹配'}
            </Tag>
            <h2>
              {uncertain ? '项目适用性需人工判断' : '本文件未进入规则审核'}
            </h2>
            <p>
              {uncertain
                ? '本文件暂未进入规则审核，已保存需人工判断的说明。'
                : '已完成项目适用性判断，并生成说明报告。'}
            </p>
          </div>
        </div>
        <div className="audit-output-status">
          <strong>
            {['completed', 'completed_with_report_error'].includes(file.status)
              ? '处理已结束'
              : file.status === 'failed'
                ? '处理异常'
                : '文件处理中'}
          </strong>
          <p>
            <CheckCircleFilled /> 说明报告已生成
          </p>
          <span
            className={
              file.word_status === 'failed' ? 'stage-failed' : 'audit-muted'
            }
          >
            {file.word_status === 'ready' && file.outputs.word_url
              ? 'Word 已就绪'
              : file.word_status === 'failed'
                ? 'Word 导出失败'
                : file.word_status === 'generating'
                  ? 'Word 生成中'
                  : 'Word 未就绪'}
          </span>
        </div>
      </section>
      <div className="audit-applicability-grid">
        <Card title="判断原因">
          <p className="audit-applicability-reason">{reason}</p>
          <div className="audit-processing-scope">
            <h3>本次处理范围</h3>
            <div>
              <span>
                文档解析与适用性判断
                <small>阶段完成不代表所有内容完整可读。</small>
              </span>
              <Tag>
                {parsed.status === 'completed' && routed.status === 'completed'
                  ? '已完成'
                  : `解析：${statusText(parsed.status)}；判断：${statusText(routed.status)}`}
              </Tag>
            </div>
            <div>
              <span>
                逐项规则审核<small>本次未产生逐条规则结论。</small>
              </span>
              <Tag>{statusText(auditing.status)}</Tag>
            </div>
            <div>
              <span>投标方资料核验</span>
              <Tag>{skippedAudit(file) ? '未执行' : '未记录'}</Tag>
            </div>
          </div>
          <p className="audit-muted">本次说明不构成逐项规则审核结论。</p>
        </Card>
        <Card title="文件与审核依据">
          <dl className="audit-basis">
            <dt>投标文件</dt>
            <dd>
              <EntityLabel id={file.file_id}>{file.file_name}</EntityLabel>
            </dd>
            <dt>规则库</dt>
            <dd>
              <EntityLabel id={file.project_id}>
                {file.project_name}
              </EntityLabel>
            </dd>
            <dt>资料库</dt>
            <dd>
              <EntityLabel id={file.bidder_id}>{file.bidder_name}</EntityLabel>
            </dd>
          </dl>
          <Alert
            type="info"
            showIcon
            title="核对提示"
            description="请确认投标文件与所选规则库属于同一采购项目。"
          />
          <p className="audit-muted">资料库已绑定，不代表已执行资料核验。</p>
        </Card>
      </div>
    </>
  );
}
