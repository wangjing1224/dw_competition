import { Alert, Button, Empty, Select, Space } from 'antd';
import type { AuditFile } from '@/services/auditTasks';
import GraphCanvas from './GraphCanvas';
import { type agentGraph, outputText, skippedAudit, stage } from './graphModel';

export default function AgentGraph({
  file,
  graph,
  onSession,
  onSelect,
  selected,
  onFlow,
  onRoute,
  partial,
  focus,
  onToggle,
}: {
  file: AuditFile;
  graph: ReturnType<typeof agentGraph>;
  onSession: (id: string) => void;
  onSelect: (id: string) => void;
  selected?: string;
  onFlow: () => void;
  onRoute?: () => void;
  partial: boolean;
  focus?: { id: string; nonce: number };
  onToggle: (id: string) => void;
}) {
  if (skippedAudit(file))
    return (
      <div className="audit-skipped-agent">
        <header>
          <div className="audit-muted">按流程跳过</div>
          <h2>
            本文件{file.route?.match_status === 'uncertain' ? '暂未' : '未'}
            进入审核 Agent
          </h2>
          <p>
            {file.route?.match_status === 'uncertain'
              ? '项目适用性需人工判断'
              : '项目适用性判断为不匹配'}
            ，因此未启动规则审核和资料核验。
          </p>
        </header>
        <section
          className="audit-skipped-reason"
          aria-labelledby="audit-skipped-reason-title"
        >
          <h3 id="audit-skipped-reason-title">判断原因</h3>
          <p>{file.route?.match_reason || '未记录判断原因'}</p>
        </section>
        <section
          className="audit-skipped-boundary"
          aria-labelledby="audit-skipped-boundary-title"
        >
          <h3 id="audit-skipped-boundary-title">本次处理范围与输出</h3>
          <dl>
            <div>
              <dt>规则审核</dt>
              <dd>已跳过</dd>
            </div>
            <div>
              <dt>资料核验</dt>
              <dd>未执行</dd>
            </div>
            <div>
              <dt>输出内容</dt>
              <dd>项目适用性说明</dd>
              <dd className="audit-skipped-output">{outputText(file)}</dd>
            </div>
          </dl>
        </section>
        <footer>
          <div>
            <p className="audit-skipped-note">跳过规则审核不代表审核不通过。</p>
            {!onRoute && (
              <p className="audit-muted">路由轨迹尚未加载或未记录。</p>
            )}
          </div>
          <Space wrap>
            <Button onClick={onFlow}>返回流程图</Button>
            <Button disabled={!onRoute} onClick={onRoute}>
              查看路由轨迹
            </Button>
          </Space>
        </footer>
      </div>
    );
  if (!graph.session)
    return (
      <div className="audit-graph-empty">
        <Empty
          description={
            stage(file, 'auditing').status === 'failed'
              ? '审核阶段执行失败，尚无可连接的 Agent 会话'
              : stage(file, 'auditing').status === 'waiting' ||
                  stage(file, 'auditing').status === 'not_run'
                ? '当前尚未进入规则审核阶段'
                : '尚未取得可连接的 Agent 会话；不根据缺失轨迹推断未执行'
          }
        />
        <Button onClick={onFlow}>查看流程状态</Button>
        {!!graph.detached.length && (
          <Alert
            type="warning"
            title="关联信息不完整，可在轨迹树中查看未连接记录"
          />
        )}
      </div>
    );
  return (
    <>
      {partial && (
        <div className="audit-graph-notice">
          仅展示已加载调用，节点次数可能尚不完整。
        </div>
      )}
      {!!graph.detached.length && (
        <div className="audit-graph-notice">
          部分记录关联信息不完整，未强行连接；可在轨迹树查看。
        </div>
      )}
      <GraphCanvas
        key={graph.session.span_id}
        graph={graph}
        label="Agent 调用关系图"
        selected={selected}
        onSelect={onSelect}
        focus={focus}
        onToggle={onToggle}
        extra={
          <Space wrap>
            <Select
              aria-label="主审核会话"
              value={graph.session.span_id}
              onChange={onSession}
              options={graph.sessions.map((s, i) => ({
                value: s.span_id,
                label: `主审核会话 ${String(i + 1).padStart(2, '0')} · ${s.status === 'running' ? '进行中' : s.status === 'failed' ? '失败' : s.status === 'completed' ? '已结束' : s.status}`,
              }))}
              style={{ width: 220 }}
            />
            <span className="audit-muted">连线表示调用归属，不表示并发</span>
          </Space>
        }
      />
    </>
  );
}
