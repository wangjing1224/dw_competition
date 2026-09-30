import { useEffect, useMemo, useState } from 'react';
import { Link, useParams } from '@umijs/max';
import {
  Alert,
  App,
  Button,
  Card,
  Empty,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
} from 'antd';
import {
  DownloadOutlined,
  EditOutlined,
  FileTextOutlined,
  PlusOutlined,
} from '@ant-design/icons';
import {
  api,
  ApiError,
  categories,
  download,
  editLibrary,
  fileSize,
  priorities,
  provenances,
  verifications,
  type Detail,
  type Rule,
} from '@/services/ruleLibraries';
import { Failure, NoRules, PageHeading, RuleCard, Status } from '../shared';
import SourcePreview from './SourcePreview';
import EntityLabel from '@/components/EntityLabel';
import { fileLabel, ruleLabels } from '@/utils/displayLabels';

const options = (values: Record<string, string>) =>
  Object.entries(values).map(([value, label]) => ({ value, label }));
type RuleDraft = Pick<
  Rule,
  'category' | 'requirement' | 'priority' | 'verification' | 'reason'
> & { source_file_ids: string[] };
export default function LibraryDetail() {
  const { projectId = '' } = useParams();
  const { message, modal } = App.useApp();
  const [data, setData] = useState<Detail>();
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const [category, setCategory] = useState('');
  const [q, setQ] = useState('');
  const [priority, setPriority] = useState<string>();
  const [verification, setVerification] = useState<string>();
  const [provenance, setProvenance] = useState<string>();
  const [source, setSource] = useState<string>();
  const [sourceOpen, setSourceOpen] = useState<string>();
  const [editing, setEditing] = useState<Rule | 'new'>();
  const [profileOpen, setProfileOpen] = useState(false);
  const [profile, setProfile] = useState('');
  const [revision, setRevision] = useState('');
  const [saveError, setSaveError] = useState('');
  const [conflict, setConflict] = useState(false);
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm<RuleDraft>();
  const draftPriority = Form.useWatch('priority', form);
  const draftVerification = Form.useWatch('verification', form);
  useEffect(() => {
    const controller = new AbortController();
    setError('');
    api<Detail>(`/${projectId}`, { signal: controller.signal })
      .then(setData)
      .catch((e) => {
        if (e.name !== 'AbortError') setError(e.message);
      });
    return () => controller.abort();
  }, [projectId, reload]);
  const editable =
    data?.project.status === 'rule_ready' && !!data.rule_set && !!data.revision;
  const rules = data?.rule_set?.rules || [];
  const labels = useMemo(() => ruleLabels(rules), [rules]);
  const sourceName = (id: string) => fileLabel(id, data?.files || []);
  const visible = useMemo(
    () =>
      rules
        .filter(
          (r) =>
            (!category || r.category === category) &&
            (!priority || r.priority === priority) &&
            (!verification || r.verification === verification) &&
            (!provenance || r.provenance === provenance) &&
            (!source || r.sources.some((s) => s.file_id === source)) &&
            `${labels.get(r.rule_id)} ${r.rule_id} ${r.requirement} ${r.reason}`
              .toLowerCase()
              .replace(/\s/g, '')
              .includes(q.toLowerCase().replace(/\s/g, '')),
        )
        .sort(
          (a, b) =>
            Object.keys(categories).indexOf(a.category) -
              Object.keys(categories).indexOf(b.category) ||
            Object.keys(priorities).indexOf(a.priority) -
              Object.keys(priorities).indexOf(b.priority),
        ),
    [rules, labels, category, priority, verification, provenance, source, q],
  );
  function openRule(rule: Rule | 'new') {
    setRevision(data?.revision || '');
    setEditing(rule);
    setSaveError('');
    setConflict(false);
    form.resetFields();
    form.setFieldsValue(
      rule === 'new'
        ? {
            category: category || undefined,
            priority: undefined,
            verification: undefined,
            requirement: '',
            reason: '',
            source_file_ids: [],
          }
        : { ...rule, source_file_ids: rule.sources.map((s) => s.file_id) },
    );
  }
  function onSaveError(e: unknown) {
    setSaveError((e as Error).message);
    // 不确定是否保存成功时先读取核对，不能自动或盲目重试新增。
    setConflict(
      !(e instanceof ApiError) || e.status === 409 || e.status >= 500,
    );
  }
  async function loadForComparison() {
    try {
      const latest = await api<Detail>(`/${projectId}`);
      const latestLabels = ruleLabels(latest.rule_set?.rules || []);
      setData(latest);
      modal.confirm({
        title: '已读取最新版本，请确认差异后再保存',
        width: 720,
        content: (
          <div>
            <p>当前草稿保留。下面是服务器已保存的内容：</p>
            <pre className="trace-content">
              {editing && editing !== 'new'
                ? JSON.stringify(
                    latest.rule_set?.rules.find(
                      (r) => r.rule_id === editing.rule_id,
                    ),
                    null,
                    2,
                  )
                : profileOpen
                  ? latest.rule_set?.project_profile
                  : latest.rule_set?.rules
                      .map(
                        (rule) =>
                          `${latestLabels.get(rule.rule_id)}：${rule.requirement}`,
                      )
                      .join('\n\n') || '当前尚无规则'}
            </pre>
          </div>
        ),
        okText: '已核对，以最新版本继续编辑',
        cancelText: '保留冲突状态',
        onOk: () => {
          setRevision(latest.revision || '');
          setConflict(false);
          setSaveError('');
        },
      });
    } catch (e) {
      onSaveError(e);
    }
  }
  async function persistRule(values: RuleDraft) {
    if (!editing) return;
    setSaving(true);
    setSaveError('');
    try {
      await editLibrary(
        projectId,
        revision,
        editing === 'new' ? 'add_rule' : 'update_rule',
        {
          ...values,
          ...(editing === 'new' ? {} : { rule_id: editing.rule_id }),
        },
      );
      setEditing(undefined);
      setReload((n) => n + 1);
      message.success('规则已保存');
    } catch (e) {
      onSaveError(e);
    } finally {
      setSaving(false);
    }
  }
  async function saveRule() {
    const values = await form.validateFields().catch(() => null);
    if (!values) return;
    const removing =
      editing &&
      editing !== 'new' &&
      editing.sources.some((s) => !values.source_file_ids.includes(s.file_id));
    modal.confirm({
      title: removing ? '确认修改并移除部分来源？' : '确认保存规则',
      width: 720,
      okText: '确认保存',
      content: (
        <div className="save-preview">
          <p>
            <strong>
              {categories[values.category]} · {priorities[values.priority]} ·{' '}
              {verifications[values.verification]}
            </strong>
          </p>
          {editing !== 'new' && editing && (
            <>
              <span className="muted">修改前</span>
              <p>{editing.requirement}</p>
            </>
          )}
          <span className="muted">保存后</span>
          <p>{values.requirement}</p>
          <p>依据说明：{values.reason || '未填写'}</p>
          <p>来源：{values.source_file_ids.map(sourceName).join('、')}</p>
        </div>
      ),
      onOk: () => persistRule(values),
    });
  }
  async function saveProfile() {
    setSaving(true);
    try {
      await editLibrary(projectId, revision, 'update_profile', {
        project_profile: profile,
      });
      setProfileOpen(false);
      setReload((n) => n + 1);
      message.success('项目简介已保存');
    } catch (e) {
      onSaveError(e);
    } finally {
      setSaving(false);
    }
  }
  const conflictNotice = (
    <>
      {saveError && (
        <Alert
          className="section-gap"
          type="error"
          title={saveError}
          action={
            conflict && (
              <Button onClick={loadForComparison}>载入最新版本并比较</Button>
            )
          }
        />
      )}
    </>
  );
  return (
    <main className="business-page">
      <PageHeading
        title={
          <EntityLabel id={projectId}>
            {data?.project.display_name || '规则库详情'}
          </EntityLabel>
        }
        actions={
          <>
            <Link to={`/rule-libraries/${projectId}/progress`}>
              <Button>执行轨迹</Button>
            </Link>
            <Button
              icon={<DownloadOutlined />}
              disabled={!editable}
              onClick={() =>
                download(
                  `/${projectId}/export?format=json`,
                  `${projectId}_规则库.json`,
                ).catch((e) => message.error(e.message))
              }
            >
              JSON
            </Button>
            <Button
              icon={<DownloadOutlined />}
              disabled={!editable}
              onClick={() =>
                download(
                  `/${projectId}/export?format=md`,
                  `${projectId}_规则库.md`,
                ).catch((e) => message.error(e.message))
              }
            >
              Markdown
            </Button>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              disabled={!editable}
              onClick={() => openRule('new')}
            >
              新增规则
            </Button>
          </>
        }
      />
      {error && <Failure error={error} retry={() => setReload((n) => n + 1)} />}
      {!data && !error && <Spin />}
      {data && (
        <>
          <Space className="section-gap" wrap>
            <Status status={data.project.status} />
            <Tag color="blue">
              {data.warning?.code === 'rule_set_unavailable'
                ? '规则数量不可读取'
                : `${rules.length} 条规则`}
            </Tag>
            <Tag>{data.files.length} 份来源文件</Tag>
            <span className="muted">
              {data.available_for_audit
                ? '可供后续审核选择'
                : '当前不可用于审核'}{' '}
              · 规则仍需人工核对
            </span>
          </Space>
          {data.warning && (
            <Alert
              className="section-gap"
              type="warning"
              showIcon
              title={data.warning.message}
            />
          )}
          {data.project.error && (
            <Alert
              type="error"
              className="section-gap"
              title={data.project.error.message}
            />
          )}
          {data.project.status !== 'rule_ready' && (
            <Alert
              className="section-gap"
              type="info"
              title="当前展示已保存的阶段结果，仅供查看，暂不能维护或正式导出。"
            />
          )}
          <Card
            className="section-gap"
            title="项目简介"
            extra={
              <Button
                type="text"
                icon={<EditOutlined />}
                disabled={!editable}
                onClick={() => {
                  setProfile(data.rule_set?.project_profile || '');
                  setRevision(data.revision || '');
                  setSaveError('');
                  setConflict(false);
                  setProfileOpen(true);
                }}
              >
                编辑简介
              </Button>
            }
          >
            <p className="profile-text">
              {data.rule_set?.project_profile || '暂无项目简介'}
            </p>
          </Card>
          <Tabs
            items={[
              {
                key: 'rules',
                label: '分类规则',
                children: (
                  <div className="review-grid">
                    <nav className="category-nav">
                      <button
                        type="button"
                        className={!category ? 'selected' : ''}
                        onClick={() => setCategory('')}
                      >
                        全部规则 <span>{rules.length}</span>
                      </button>
                      {Object.entries(categories).map(([key, label]) => (
                        <button
                          type="button"
                          className={category === key ? 'selected' : ''}
                          key={key}
                          onClick={() => setCategory(key)}
                        >
                          {label}
                          <span>
                            {rules.filter((r) => r.category === key).length}
                          </span>
                        </button>
                      ))}
                    </nav>
                    <section>
                      <div className="rule-filters">
                        <Input.Search
                          allowClear
                          placeholder="搜索规则编号、内容或依据说明"
                          value={q}
                          onChange={(e) => setQ(e.target.value)}
                        />
                        <Select
                          allowClear
                          placeholder="重要程度"
                          options={options(priorities)}
                          value={priority}
                          onChange={setPriority}
                        />
                        <Select
                          allowClear
                          placeholder="核验方式"
                          options={options(verifications)}
                          value={verification}
                          onChange={setVerification}
                        />
                        <Select
                          allowClear
                          placeholder="生成来源"
                          options={options(provenances)}
                          value={provenance}
                          onChange={setProvenance}
                        />
                        <Select
                          allowClear
                          placeholder="来源文件"
                          options={data.files.map((f) => ({
                            label: sourceName(f.file_id),
                            value: f.file_id,
                          }))}
                          value={source}
                          onChange={setSource}
                        />
                      </div>
                      <div className="result-label">
                        {category ? categories[category] : '全部分类'} ·{' '}
                        {visible.length} 条
                      </div>
                      {visible.map((rule) => (
                        <RuleCard
                          key={rule.rule_id}
                          rule={rule}
                          label={labels.get(rule.rule_id)}
                          onSource={setSourceOpen}
                          onEdit={editable ? () => openRule(rule) : undefined}
                        />
                      ))}
                      {data.warning?.code === 'rule_set_unavailable' ? (
                        <Empty description="规则快照不可读取，请切换到来源文件查看材料" />
                      ) : !rules.length ? (
                        <NoRules />
                      ) : (
                        !visible.length && (
                          <Empty description="暂无符合筛选条件的规则" />
                        )
                      )}
                    </section>
                  </div>
                ),
              },
              {
                key: 'sources',
                label: '来源文件',
                children: (
                  <Table
                    rowKey="file_id"
                    dataSource={data.files}
                    pagination={false}
                    scroll={{ x: 800 }}
                    columns={[
                      {
                        title: '文件',
                        render: (_, file) => (
                          <div>
                            <strong>
                              <FileTextOutlined />{' '}
                              <EntityLabel id={file.file_id}>
                                {file.file_name}
                              </EntityLabel>
                            </strong>
                            <div className="muted">{file.source_path}</div>
                          </div>
                        ),
                      },
                      {
                        title: '大小',
                        render: (_, f) => fileSize(f.size_bytes),
                      },
                      {
                        title: '关联规则',
                        render: (_, f) =>
                          rules.filter((r) =>
                            r.sources.some((s) => s.file_id === f.file_id),
                          ).length,
                      },
                      {
                        title: '操作',
                        render: (_, f) => (
                          <Space>
                            <Button
                              type="link"
                              onClick={() => setSourceOpen(f.file_id)}
                            >
                              查看来源
                            </Button>
                            <Button
                              type="link"
                              onClick={() =>
                                download(
                                  `/${projectId}/files/${f.file_id}/download`,
                                  f.file_name,
                                ).catch((e) => message.error(e.message))
                              }
                            >
                              下载
                            </Button>
                          </Space>
                        ),
                      },
                    ]}
                  />
                ),
              },
            ]}
          />
        </>
      )}
      <SourcePreview
        projectId={projectId}
        fileId={sourceOpen}
        close={() => setSourceOpen(undefined)}
      />
      <Modal
        title={
          editing === 'new' ? (
            '人工新增规则'
          ) : editing ? (
            <EntityLabel id={editing.rule_id}>
              编辑{labels.get(editing.rule_id) || '规则'}
            </EntityLabel>
          ) : (
            '编辑规则'
          )
        }
        open={!!editing}
        width={760}
        onCancel={() => {
          if (!saving) setEditing(undefined);
        }}
        onOk={saveRule}
        confirmLoading={saving}
        okButtonProps={{
          disabled:
            conflict ||
            (draftPriority === 'normal' &&
              draftVerification === 'evidence_check'),
        }}
        destroyOnHidden
      >
        <Form form={form} layout="vertical">
          <div className="edit-grid">
            <Form.Item
              name="category"
              label="分类"
              rules={[{ required: true }]}
            >
              <Select options={options(categories)} />
            </Form.Item>
            <Form.Item
              name="priority"
              label="重要程度"
              rules={[{ required: true }]}
            >
              <Select options={options(priorities)} />
            </Form.Item>
            <Form.Item
              name="verification"
              label="核验方式"
              rules={[{ required: true }]}
            >
              <Select options={options(verifications)} />
            </Form.Item>
          </div>
          {draftPriority === 'normal' &&
            draftVerification === 'evidence_check' && (
              <Alert
                type="warning"
                title="资料核验规则至少选择重要，请明确调整重要程度后保存。"
              />
            )}
          <Form.Item
            name="requirement"
            label="规则要求"
            rules={[
              { required: true, whitespace: true, message: '请填写具体要求' },
            ]}
          >
            <Input.TextArea autoSize={{ minRows: 4, maxRows: 12 }} />
          </Form.Item>
          <Form.Item
            name="reason"
            label="依据说明"
            help="解释要求依据和适用范围，不必重复规则正文。"
          >
            <Input.TextArea rows={3} />
          </Form.Item>
          <Form.Item
            name="source_file_ids"
            label="来源文件"
            rules={[
              {
                required: true,
                type: 'array',
                min: 1,
                message: '至少选择一份来源文件',
              },
            ]}
          >
            <Select
              mode="multiple"
              options={data?.files.map((f) => ({
                label: sourceName(f.file_id),
                value: f.file_id,
              }))}
            />
          </Form.Item>
        </Form>
        {conflictNotice}
      </Modal>
      <Modal
        title="编辑项目简介"
        open={profileOpen}
        width={760}
        onCancel={() => {
          if (!saving) setProfileOpen(false);
        }}
        onOk={saveProfile}
        confirmLoading={saving}
        okButtonProps={{ disabled: conflict }}
      >
        <Input.TextArea
          autoSize={{ minRows: 8, maxRows: 18 }}
          value={profile}
          onChange={(e) => setProfile(e.target.value)}
        />
        {conflictNotice}
      </Modal>
    </main>
  );
}
