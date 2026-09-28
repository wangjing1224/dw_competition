import {
  DeleteOutlined,
  FileAddOutlined,
  FolderOpenOutlined,
  InboxOutlined,
} from '@ant-design/icons';
import { history } from '@umijs/max';
import {
  Alert,
  App,
  Button,
  Card,
  Input,
  Space,
  Steps,
  Table,
  Tag,
} from 'antd';
import { useEffect, useRef, useState } from 'react';
import {
  type PendingFile,
  validateFiles,
} from '@/components/LibraryUpload/files';
import {
  ApiError,
  api,
  type Creation,
  fileSize,
  listLibraries,
} from '@/services/ruleLibraries';
import { PageHeading } from '../shared';

export { validateFiles } from '@/components/LibraryUpload/files';

const storageKey = 'rule-library-pending-submission';
export default function CreateLibrary() {
  const { message, modal } = App.useApp();
  const fileInput = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<PendingFile[]>([]);
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const [pendingId, setPendingId] = useState<string | null>(() =>
    sessionStorage.getItem(storageKey),
  );
  const [canRetry, setCanRetry] = useState(false);
  const locked = busy || (!!pendingId && !canRetry);
  useEffect(() => {
    if (!busy) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [busy]);
  const addFiles = (incoming: File[]) => {
    const checked = validateFiles(incoming, files);
    setFiles([...files, ...checked.accepted]);
    setErrors(checked.errors);
  };
  const go = (data: { project_id: string; status: string }) => {
    sessionStorage.removeItem(storageKey);
    setPendingId(null);
    history.push(
      `/rule-libraries/${data.project_id}${data.status === 'processing_rules' ? '/progress' : ''}`,
    );
  };
  async function recover() {
    setBusy(true);
    try {
      const result = await listLibraries({
        client_request_id: pendingId || '',
      });
      if (result.items[0]) go(result.items[0]);
      else {
        setCanRetry(true);
        message.info(
          '尚未查到接收结果。可再次查询，或确认后以同一编号重试；名称及文件必须与上次一致。',
        );
      }
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function submit() {
    if ((pendingId && !canRetry) || busy || !files.length || !name.trim())
      return;
    const requestId = pendingId || crypto.randomUUID();
    setPendingId(requestId);
    sessionStorage.setItem(storageKey, requestId);
    setBusy(true);
    setCanRetry(false);
    setErrors([]);
    const form = new FormData();
    form.append('display_name', name.trim());
    form.append('client_request_id', requestId);
    files.forEach(({ file, path }) => {
      form.append('files', file);
      form.append('relative_paths', path);
    });
    try {
      go(await api<Creation>('', { method: 'POST', body: form }));
    } catch (e) {
      const error = e as Error;
      setErrors([error.message]);
      if (
        e instanceof ApiError &&
        e.status < 500 &&
        !['upload_in_progress', 'upload_request_conflict'].includes(e.code)
      ) {
        sessionStorage.removeItem(storageKey);
        setPendingId(null);
      }
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="business-page">
      <PageHeading
        title="新建规则库"
        description="一次导入同一招标项目的全部材料，保留目录结构与来源信息。"
      />
      <Steps
        className="creation-steps"
        current={0}
        items={[
          { title: '导入招标材料' },
          { title: 'AI 整理规则' },
          { title: '审阅与维护' },
        ]}
      />
      {pendingId && (
        <Alert
          className="section-gap"
          type="warning"
          showIcon
          title="正在确认提交结果"
          description="提交标识已保留。刷新页面也不会自动重建，请先查询此次上传是否已经接收。"
          action={
            <Space direction="vertical">
              <Button loading={busy} onClick={recover}>
                查询提交结果
              </Button>
              {canRetry && (
                <Button
                  disabled={busy}
                  onClick={() =>
                    modal.confirm({
                      title: '明确开始一次新的提交？',
                      content:
                        '旧请求仍可能在接收，建议优先按原编号重试。新编号可能创建另一个规则库；仅在确认要重新提交时继续。',
                      okText: '开始新提交',
                      onOk: () => {
                        sessionStorage.removeItem(storageKey);
                        setPendingId(null);
                        setCanRetry(false);
                      },
                    })
                  }
                >
                  开始新的提交
                </Button>
              )}
            </Space>
          }
        />
      )}
      <div className="create-grid">
        <div>
          <Card title="规则库信息">
            <label className="field-label" htmlFor="library-name">
              规则库名称 <span className="required">*</span>
            </label>
            <Input
              id="library-name"
              maxLength={120}
              showCount
              size="large"
              placeholder="例如：2026 年红外检测设备采购"
              value={name}
              disabled={locked}
              onChange={(e) => setName(e.target.value)}
            />
            <small className="muted">规则库名称不能与已有规则库重复。</small>
          </Card>
          <Card className="section-gap" title="招标材料">
            <div
              className="upload-zone"
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                if (!locked) addFiles(Array.from(e.dataTransfer.files));
              }}
            >
              <InboxOutlined />
              <h3>选择文件或文件夹，也可拖入文件</h3>
              <p>
                DOC / DOCX / PDF / XLSX / TXT / MD / PNG / JPG · 单份不超过 100
                MiB
              </p>
              <Space>
                <Button
                  icon={<FileAddOutlined />}
                  onClick={() => fileInput.current?.click()}
                  disabled={locked}
                >
                  选择文件
                </Button>
                <Button
                  icon={<FolderOpenOutlined />}
                  onClick={() => folderInput.current?.click()}
                  disabled={locked}
                >
                  选择文件夹
                </Button>
              </Space>
              <input
                ref={fileInput}
                hidden
                type="file"
                multiple
                accept=".doc,.docx,.pdf,.xlsx,.txt,.md,.png,.jpg,.jpeg"
                onChange={(e) => {
                  addFiles(Array.from(e.target.files || []));
                  e.target.value = '';
                }}
              />
              <input
                ref={folderInput}
                hidden
                type="file"
                multiple
                {...{ webkitdirectory: '' }}
                onChange={(e) => {
                  addFiles(Array.from(e.target.files || []));
                  e.target.value = '';
                }}
              />
            </div>
            {errors.length > 0 && (
              <Alert
                className="section-gap"
                type="error"
                title="请检查以下内容"
                description={errors.map((e) => <div key={e}>{e}</div>)}
              />
            )}
            <div className="selection-summary">
              <strong>已选择 {files.length} 份文件</strong>
              <span>
                {fileSize(files.reduce((sum, row) => sum + row.file.size, 0))}
              </span>
            </div>
            <Table
              rowKey="path"
              size="small"
              dataSource={files}
              pagination={{ pageSize: 8, showSizeChanger: false }}
              locale={{ emptyText: '还未选择招标文件' }}
              columns={[
                {
                  title: '相对路径',
                  dataIndex: 'path',
                  render: (path) => <span className="file-path">{path}</span>,
                },
                {
                  title: '大小',
                  width: 110,
                  render: (_, row) => fileSize(row.file.size),
                },
                {
                  title: '',
                  width: 56,
                  render: (_, row) => (
                    <Button
                      aria-label={`移除 ${row.path}`}
                      type="text"
                      icon={<DeleteOutlined />}
                      disabled={locked}
                      onClick={() =>
                        setFiles(files.filter((f) => f.path !== row.path))
                      }
                    />
                  ),
                },
              ]}
            />
          </Card>
        </div>
        <aside>
          <Card title="将生成什么">
            <Tag color="blue">规则库</Tag>
            <h3>一份可维护的审核依据</h3>
            <p className="muted">
              归纳项目简介，整理六类规则，并保留每条规则的来源文件。
            </p>
            <ul className="feature-list">
              <li>保留要求中的数值、范围与条件</li>
              <li>区分重要程度和核验方式</li>
              <li>可查看解析原文、图片与执行轨迹</li>
              <li>支持人工新增与修订</li>
            </ul>
            <Alert
              type="info"
              title="上传完成不等于处理完成"
              description="材料接收后进入后台解析、索引和规则抽取，可在进度页持续查看。"
            />
          </Card>
        </aside>
      </div>
      <div className="sticky-actions">
        <span className="muted">本次将创建独立规则库，不合并到已有项目。</span>
        <Button
          type="primary"
          size="large"
          loading={busy}
          disabled={!files.length || !name.trim() || locked}
          onClick={() =>
            pendingId
              ? modal.confirm({
                  title: '以原提交编号重试？',
                  content:
                    '请确认名称、文件顺序、目录和内容与原提交一致。服务端将复用已有项目，不会重复排队。',
                  okText: '确认重试',
                  onOk: submit,
                })
              : submit()
          }
        >
          {busy
            ? '正在接收文件…'
            : pendingId
              ? '使用原编号重试上传'
              : '创建并开始整理'}
        </Button>
      </div>
    </main>
  );
}
