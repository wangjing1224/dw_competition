import {
  ArrowRightOutlined,
  AuditOutlined,
  FolderOpenOutlined,
  PlusOutlined,
  ReadOutlined,
} from '@ant-design/icons';
import { Link } from '@umijs/max';
import { Button, Tag } from 'antd';
import { PageHeading } from '../RuleLibraries/shared';

export default function Workbench() {
  return (
    <main className="business-page">
      <PageHeading
        title="招投标智能审核工作台"
        description="让招标要求成为可复用规则，让每一项审核结论有据可查。"
        back={false}
      />
      <section className="home-hero">
        <div>
          <span className="eyebrow">BID REVIEW WORKSPACE</span>
          <h2>从招标要求，到有据可查的审核</h2>
          <p>整理项目规则与投标方证明材料，按文件核对响应情况。</p>
          <Link to="/rule-libraries/new">
            <Button type="primary" size="large" icon={<PlusOutlined />}>
              新建规则库
            </Button>
          </Link>
        </div>
        <div className="hero-emblem" aria-hidden>
          <ReadOutlined />
          <span>规则 · 材料 · 审核</span>
        </div>
      </section>
      <div className="home-flow">
        <div className="resource-stack">
          <section className="module-card">
            <div className="module-icon">
              <ReadOutlined />
            </div>
            <div>
              <Tag color="blue">可用</Tag>
              <h2>规则库</h2>
              <p>
                导入招标文件，提炼审核要求。按六类审阅规则，回查来源并进行人工修订。
              </p>
              <SpaceLinks />
            </div>
          </section>
          <section className="module-card">
            <div className="module-icon">
              <FolderOpenOutlined />
            </div>
            <div>
              <Tag color="blue">可用</Tag>
              <h2>投标方资料库</h2>
              <p>独立维护资质、合同与证明材料，为核验提供可追溯依据。</p>
              <div className="module-actions">
                <Link to="/evidence-libraries/new">
                  <Button type="primary">新建资料库</Button>
                </Link>
                <Link to="/evidence-libraries">
                  管理资料库 <ArrowRightOutlined />
                </Link>
              </div>
            </div>
          </section>
        </div>
        <div className="flow-connector" aria-hidden>
          <span>规则与证明材料</span>
          <ArrowRightOutlined />
        </div>
        <section className="module-card audit-module">
          <div className="module-icon">
            <AuditOutlined />
          </div>
          <Tag color="blue">可用</Tag>
          <h2>审核任务</h2>
          <p>为每份投标文件选择规则库和投标方资料，逐份生成审核报告。</p>
          <div className="audit-capabilities">
            <span>按规则核对</span>
            <span>资料核验</span>
            <span>疑点与来源</span>
          </div>
          <div className="module-actions">
            <Link to="/audit-tasks/new">
              <Button type="primary">创建审核任务</Button>
            </Link>
            <Link to="/audit-tasks">
              管理审核任务 <ArrowRightOutlined />
            </Link>
          </div>
        </section>
      </div>
    </main>
  );
}
function SpaceLinks() {
  return (
    <div className="module-actions">
      <Link to="/rule-libraries/new">
        <Button type="primary">新建规则库</Button>
      </Link>
      <Link to="/rule-libraries">
        管理规则库 <ArrowRightOutlined />
      </Link>
    </div>
  );
}
