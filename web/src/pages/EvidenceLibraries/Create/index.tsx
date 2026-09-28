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
} from '@/services/evidenceLibraries';
import { Heading as PageHeading } from '../shared';

const storageKey = 'evidence-library-pending-submission';
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
  const go = (data: { bidder_id: string; status: string }) => {
    sessionStorage.removeItem(storageKey);
    setPendingId(null);
    history.push(
      `/evidence-libraries/${data.bidder_id}${['uploading_evidence', 'processing_evidence'].includes(data.status) ? '/progress' : ''}`,
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
        title="新建资料库"
        description="一次导入同一投标方的全部材料，保留目录结构与来源信息。"
      />
      <Steps
        className="creation-steps"
        current={0}
        items={[
          { title: '导入投标方材料' },
          { title: 'AI 整理资料' },
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
                        '旧请求仍可能在接收，建议优先按原编号重试。新编号可能创建另一个资料库；仅在确认要重新提交时继续。',
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
      <div className="evidence-create">
        <div>
          <Card title="资料库信息">
            <label className="field-label" htmlFor="library-name">
              资料库名称 <span className="required">*</span>
            </label>
            <Input
              id="library-name"
              maxLength={100}
              showCount
              size="large"
              placeholder="例如：华衡科技 · 投标证明资料"
              value={name}
              disabled={locked}
              onChange={(e) => setName(e.target.value)}
            />
            <small className="muted">资料库名称不能与已有资料库重复。</small>
          </Card>
          <Card className="section-gap" title="投标方材料">
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
              <Space wrap>
                {Object.entries(
                  files.reduce<Record<string, number>>((counts, row) => {
                    const ext =
                      row.file.name.split('.').pop()?.toUpperCase() || '';
                    counts[ext] = (counts[ext] || 0) + 1;
                    return counts;
                  }, {}),
                ).map(([ext, count]) => (
                  <Tag key={ext}>
                    {ext} {count}
                  </Tag>
                ))}
              </Space>
              <span>
                {fileSize(files.reduce((sum, row) => sum + row.file.size, 0))}
              </span>
            </div>
            <Table
              rowKey="path"
              size="small"
              dataSource={files}
              pagination={false}
              scroll={{ y: 320 }}
              locale={{ emptyText: '还未选择证明材料' }}
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
      </div>
      <div className="sticky-actions">
        <span className="muted">
          本次材料归属同一投标方；不同投标方请分别新建。
        </span>
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
                    '请确认名称、文件顺序、目录和内容与原提交一致。服务端将复用已有资料库，不会重复排队。',
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
