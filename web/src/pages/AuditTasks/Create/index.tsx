import { history } from '@umijs/max';
import {
  Alert,
  App,
  Button,
  Card,
  Input,
  Modal,
  Select,
  Space,
  Steps,
  Table,
  Tag,
} from 'antd';
import { FileAddOutlined, FolderOpenOutlined } from '@ant-design/icons';
import { useEffect, useRef, useState } from 'react';
import { validateFiles } from '@/components/LibraryUpload/files';
import type { PendingFile } from '@/components/LibraryUpload/files';
import { PageHeading } from '@/pages/RuleLibraries/shared';
import { fileSize } from '@/services/ruleLibraries';
import { requestJSON } from '@/services/libraryClient';
import { api } from '@/services/auditTasks';
import type { TaskList } from '@/services/auditTasks';
import '../shared';

type Library = { id: string; name: string; revision: string };
type Row = PendingFile & { project?: Library; bidder?: Library };
const pendingKey = 'audit-pending-submission';
type LibraryList = {
  items: {
    project_id?: string;
    bidder_id?: string;
    display_name: string;
    available_for_audit: boolean;
  }[];
  total: number;
};
export function LibrarySelect({
  kind,
  value,
  onChange,
  disabled,
}: {
  kind: 'project' | 'bidder';
  value?: Library;
  onChange: (v: Library) => void;
  disabled?: boolean;
}) {
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const [options, setOptions] = useState<LibraryList['items']>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [opened, setOpened] = useState(false);
  const choice = useRef(0);
  const { message } = App.useApp();
  const base = kind === 'project' ? '/api/projects' : '/api/bidders';
  useEffect(() => {
    if (!opened) return;
    const c = new AbortController();
    setOptions([]);
    setLoading(true);
    const timer = setTimeout(() => {
      requestJSON<LibraryList>(
        `${base}?${new URLSearchParams({ q: query, page: String(page), page_size: '20' })}`,
        { signal: c.signal },
      )
        .then((d) => {
          if (!c.signal.aborted) {
            setOptions(d.items);
            setTotal(d.total);
          }
        })
        .catch((e) => {
          if (!c.signal.aborted) message.error(e.message);
        })
        .finally(() => {
          if (!c.signal.aborted) setLoading(false);
        });
    }, 200);
    return () => {
      clearTimeout(timer);
      c.abort();
    };
  }, [base, query, page, message, opened]);
  const choose = async (id: string) => {
    const sequence = ++choice.current;
    setLoading(true);
    try {
      const d = await requestJSON<{
        revision: string | null;
        available_for_audit: boolean;
        project?: { display_name: string };
        bidder?: { display_name: string };
      }>(`${base}/${id}`);
      if (!d.available_for_audit || !d.revision)
        throw new Error('此库尚不可用于审核');
      if (sequence !== choice.current) return;
      onChange({
        id,
        name: (d.project || d.bidder)?.display_name || id,
        revision: d.revision,
      });
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setLoading(false);
    }
  };
  const list = options.map((o) => ({
    value: o.project_id || o.bidder_id || '',
    label: o.display_name,
    disabled: !o.available_for_audit,
  }));
  if (value && !list.some((o) => o.value === value.id))
    list.unshift({
      value: value.id,
      label: value.name,
      disabled: false,
    });
  return (
    <Select
      disabled={disabled}
      onOpenChange={setOpened}
      style={{ minWidth: 190 }}
      showSearch={{
        onSearch: (q) => {
          setQuery(q);
          setPage(1);
        },
        filterOption: false,
      }}
      placeholder={kind === 'project' ? '选择规则库' : '选择资料库'}
      value={value?.id}
      options={list}
      loading={loading}
      onChange={choose}
      popupRender={(menu) => (
        <>
          {menu}
          <Space style={{ padding: 8 }}>
            <Button
              size="small"
              disabled={page <= 1}
              onClick={() => setPage((p) => p - 1)}
            >
              上一页
            </Button>
            <span>
              {page} / {Math.max(1, Math.ceil(total / 20))}
            </span>
            <Button
              size="small"
              disabled={page * 20 >= total}
              onClick={() => setPage((p) => p + 1)}
            >
              下一页
            </Button>
          </Space>
        </>
      )}
    />
  );
}
export default function CreateAudit() {
  const { message } = App.useApp();
  const [name, setName] = useState('');
  const [rows, setRows] = useState<Row[]>([]);
  const [selected, setSelected] = useState<React.Key[]>([]);
  const [bulkProject, setBulkProject] = useState<Library>();
  const [bulkBidder, setBulkBidder] = useState<Library>();
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(
    !!sessionStorage.getItem(pendingKey),
  );
  const [error, setError] = useState('');
  const [issues, setIssues] = useState<string[]>([]);
  const requestId = useRef<string | null>(sessionStorage.getItem(pendingKey));
  const filesRef = useRef<HTMLInputElement>(null);
  const folderRef = useRef<HTMLInputElement>(null);
  const locked = busy || uncertain;
  const invalid = rows.filter((r) => !r.project || !r.bidder).length;
  const add = (files: File[]) => {
    if (locked) return;
    const result = validateFiles(files, rows);
    setRows((r) => [...r, ...result.accepted]);
    setIssues(result.errors);
    requestId.current = null;
  };
  const edit = (path: string, patch: Partial<Row>) => {
    setRows((r) => r.map((v) => (v.path === path ? { ...v, ...patch } : v)));
    requestId.current = null;
  };
  const recover = async () => {
    if (!requestId.current) return false;
    const found = await api<TaskList>(
      `?client_request_id=${requestId.current}`,
    );
    if (found.request_state === 'accepted' && found.items[0]) {
      sessionStorage.removeItem(pendingKey);
      history.push(`/audit-tasks/${found.items[0].task_id}/progress`);
      return true;
    }
    if (found.request_state === 'receiving')
      throw new Error('服务器仍在接收，请稍后再次查询，勿重复提交。');
    if (found.request_state === 'failed') {
      sessionStorage.removeItem(pendingKey);
      setUncertain(false);
      requestId.current = null;
      throw new Error(
        found.request_error?.message || '上次接收失败，请确认后重新提交。',
      );
    }
    setUncertain(false);
    sessionStorage.removeItem(pendingKey);
    return false;
  };
  const submit = async () => {
    setBusy(true);
    setError('');
    try {
      if (requestId.current && (await recover())) return;
      requestId.current ??= crypto.randomUUID();
      sessionStorage.setItem(pendingKey, requestId.current);
      const body = new FormData();
      body.set('display_name', name.trim());
      body.set('client_request_id', requestId.current);
      body.set(
        'assignments',
        JSON.stringify(
          rows.map((r, i) => ({
            upload_index: i,
            project_id: r.project?.id,
            bidder_id: r.bidder?.id,
            project_revision: r.project?.revision,
            bidder_revision: r.bidder?.revision,
          })),
        ),
      );
      rows.forEach((r) => {
        body.append('files', r.file);
        body.append('relative_paths', r.path);
      });
      const result = await api<{ task_id: string }>('', {
        method: 'POST',
        body,
      });
      sessionStorage.removeItem(pendingKey);
      history.push(`/audit-tasks/${result.task_id}/progress`);
    } catch (e) {
      const err = e as Error & { status?: number };
      setError(err.message);
      if (!err.status || err.status >= 500) {
        setUncertain(true);
      } else {
        sessionStorage.removeItem(pendingKey);
        requestId.current = null;
        setUncertain(false);
      }
    } finally {
      setBusy(false);
    }
  };
  const confirm = () =>
    Modal.confirm({
      title: `提交 ${rows.length} 份投标文件？`,
      content:
        '每份文件将按所选规则库和资料库独立审核。提交后依据版本固定，不能修改绑定。',
      okText: '确认并开始审核',
      cancelText: '继续配置',
      onOk: submit,
    });
  const refresh = async () => {
    setBusy(true);
    try {
      const updated = await Promise.all(
        rows.map(async (r) => {
          const values = await Promise.all(
            (['project', 'bidder'] as const).map(async (k) => {
              const old = r[k];
              if (!old) return undefined;
              const d = await requestJSON<{
                revision: string | null;
                available_for_audit: boolean;
              }>(`/api/${k === 'project' ? 'projects' : 'bidders'}/${old.id}`);
              return d.available_for_audit && d.revision
                ? { ...old, revision: d.revision }
                : undefined;
            }),
          );
          return { ...r, project: values[0], bidder: values[1] };
        }),
      );
      setRows(updated);
      requestId.current = null;
      message.success('已刷新所选版本，请核对后重新提交');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="business-page audit-page">
      <PageHeading
        title="新建审核任务"
        description="一次提交多份投标文件；每份文件分别选择唯一的规则库和投标方资料库。"
        backTo="/audit-tasks"
        backLabel="返回审核任务"
      />
      <Card>
        {uncertain && (
          <Alert
            type="warning"
            title="存在待确认的提交"
            description="请先查询提交状态；页面刷新不会自动重新上传或重新执行审核。"
          />
        )}
        <Steps
          size="small"
          current={!rows.length ? 0 : invalid ? 1 : 2}
          items={[
            { title: '选择文件' },
            { title: '配置审核依据' },
            { title: '确认提交' },
          ]}
        />
        <div className="section-gap">
          <label htmlFor="audit-name">任务名称</label>
          <Input
            id="audit-name"
            className="section-gap"
            maxLength={100}
            showCount
            value={name}
            disabled={locked}
            placeholder="例如：红外热成像仪采购 · 第一轮审核"
            onChange={(e) => {
              setName(e.target.value);
              requestId.current = null;
            }}
          />
        </div>
        <div
          className="audit-upload"
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => {
            e.preventDefault();
            add([...e.dataTransfer.files]);
          }}
        >
          <p>拖入投标文件，或选择整个文件夹</p>
          <Space>
            <Button
              disabled={locked}
              icon={<FileAddOutlined />}
              onClick={() => filesRef.current?.click()}
            >
              选择文件
            </Button>
            <Button
              disabled={locked}
              icon={<FolderOpenOutlined />}
              onClick={() => folderRef.current?.click()}
            >
              选择文件夹
            </Button>
          </Space>
          <p className="audit-muted">
            支持 PDF、Word、Excel、文本和图片；单文件不超过 100 MiB
          </p>
          <input
            ref={filesRef}
            type="file"
            multiple
            hidden
            onChange={(e) => {
              add([...(e.target.files || [])]);
              e.target.value = '';
            }}
          />
          <input
            ref={folderRef}
            type="file"
            multiple
            {...{ webkitdirectory: '' }}
            hidden
            onChange={(e) => {
              add([...(e.target.files || [])]);
              e.target.value = '';
            }}
          />
        </div>
        {!!issues.length && (
          <Alert
            type="warning"
            title="部分文件未加入"
            description={issues.join('；')}
          />
        )}
        <div className="audit-toolbar">
          <Space wrap>
            <span>批量配置 {selected.length} 项</span>
            <LibrarySelect
              kind="project"
              value={bulkProject}
              onChange={setBulkProject}
              disabled={locked}
            />
            <LibrarySelect
              kind="bidder"
              value={bulkBidder}
              onChange={setBulkBidder}
              disabled={locked}
            />
            <Button
              disabled={
                locked || !selected.length || (!bulkProject && !bulkBidder)
              }
              onClick={() => {
                setRows((r) =>
                  r.map((v) =>
                    selected.includes(v.path)
                      ? {
                          ...v,
                          ...(bulkProject && { project: bulkProject }),
                          ...(bulkBidder && { bidder: bulkBidder }),
                        }
                      : v,
                  ),
                );
                requestId.current = null;
              }}
            >
              应用到所选
            </Button>
          </Space>
          <Button disabled={locked || !rows.length} onClick={refresh}>
            刷新所选库版本
          </Button>
        </div>
        <div className="audit-config">
          <Table<Row>
            rowKey="path"
            dataSource={rows}
            pagination={false}
            scroll={{ x: 920 }}
            rowSelection={{
              selectedRowKeys: selected,
              onChange: setSelected,
              getCheckboxProps: () => ({ disabled: locked }),
            }}
            columns={[
              {
                title: '投标文件 / 相对路径',
                width: 260,
                render: (_, r) => (
                  <>
                    <strong>{r.file.name}</strong>
                    <div className="audit-file-path">{r.path}</div>
                  </>
                ),
              },
              {
                title: '大小',
                width: 90,
                render: (_, r) => fileSize(r.file.size),
              },
              {
                title: '规则库',
                render: (_, r) => (
                  <LibrarySelect
                    kind="project"
                    value={r.project}
                    onChange={(v) => edit(r.path, { project: v })}
                    disabled={locked}
                  />
                ),
              },
              {
                title: '投标方资料库',
                render: (_, r) => (
                  <LibrarySelect
                    kind="bidder"
                    value={r.bidder}
                    onChange={(v) => edit(r.path, { bidder: v })}
                    disabled={locked}
                  />
                ),
              },
              {
                title: '配置',
                width: 95,
                render: (_, r) => (
                  <Tag color={r.project && r.bidder ? 'green' : 'orange'}>
                    {r.project && r.bidder ? '已配置' : '待配置'}
                  </Tag>
                ),
              },
              {
                title: '操作',
                width: 70,
                render: (_, r) => (
                  <Button
                    type="link"
                    disabled={locked}
                    onClick={() => {
                      setRows((v) => v.filter((f) => f.path !== r.path));
                      setSelected((v) => v.filter((k) => k !== r.path));
                      requestId.current = null;
                    }}
                  >
                    移除
                  </Button>
                ),
              },
            ]}
          />
        </div>
        {error && (
          <Alert
            className="section-gap"
            type="error"
            title={error}
            description="文件与配置仍保留。库版本冲突时刷新并核对；网络不确定时先查询受理状态。"
          />
        )}
        <div className="audit-config-footer">
          <Space wrap>
            <span>{rows.length} 份文件</span>
            <Tag color={invalid ? 'orange' : 'green'}>
              {invalid ? `${invalid} 项待配置` : '关联检查通过'}
            </Tag>
            <span className="audit-muted">提交后由服务端复核可用性和版本</span>
          </Space>
          {uncertain ? (
            <Button
              loading={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  if (!(await recover())) setError('尚未受理，可以重新提交。');
                } catch (e) {
                  setError((e as Error).message);
                } finally {
                  setBusy(false);
                }
              }}
            >
              查询提交状态
            </Button>
          ) : (
            <Button
              type="primary"
              loading={busy}
              disabled={!name.trim() || !rows.length || !!invalid}
              onClick={confirm}
            >
              开始审核
            </Button>
          )}
        </div>
      </Card>
    </div>
  );
}
