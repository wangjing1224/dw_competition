// biome-ignore-all lint/security/noDangerouslySetInnerHtml: 只渲染共用 safeMarkdown 净化后的 HTML。

import { Link, useParams } from '@umijs/max';
import {
  Alert,
  App,
  Button,
  Card,
  Drawer,
  Empty,
  Image,
  Input,
  Modal,
  Select,
  Space,
  Spin,
} from 'antd';
import { useCallback, useEffect, useState } from 'react';
import EntityLabel from '@/components/EntityLabel';
import SourcePreview, {
  safeMarkdown,
  validImageUrl,
} from '@/components/SourcePreview';
import {
  ApiError,
  api,
  type Detail,
  download,
  editLibrary,
  type Material,
  type Preview,
} from '@/services/evidenceLibraries';
import { fileLabel } from '@/utils/displayLabels';
import { Failure, Heading, Provenance, Status } from '../shared';
import './editor.less';

type Draft = Omit<Material, 'provenance'>;
const parseLabels: Record<string, string> = {
  parsed: '解析完成',
  partial: '部分可读',
  pending: '等待解析',
  unsupported: '不支持',
  ocr_required: '需识图',
  manual_review: '需人工查看',
};
const ingestionLabels: Record<string, string> = {
  pending: '等待整理',
  processing: '正在整理',
  completed: '整理完成',
  failed: '整理中断',
};
export function directRelations(materials: Material[], fileId: string) {
  return materials.flatMap((material) =>
    material.relations
      .filter(([, target]) => material.file_id === fileId || target === fileId)
      .map(([relation, target]) => ({
        from: material.file_id,
        to: target,
        relation,
      })),
  );
}

export default function LibraryDetail() {
  const { bidderId = '' } = useParams();
  const { message, modal } = App.useApp();
  const [data, setData] = useState<Detail>();
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const [selected, setSelected] = useState<string>(
    () => new URLSearchParams(window.location.search).get('file') || '',
  );
  const [q, setQ] = useState('');
  const [filter, setFilter] = useState('all');
  const [previewFile, setPreviewFile] = useState<string>();
  const [zoom, setZoom] = useState<string>();
  const [draft, setDraft] = useState<Draft>();
  const [relationKeys, setRelationKeys] = useState<string[]>([]);
  const [original, setOriginal] = useState<Draft>();
  const [editRevision, setEditRevision] = useState('');
  const [editError, setEditError] = useState('');
  const [conflict, setConflict] = useState(false);
  const [saving, setSaving] = useState(false);
  const [name, setName] = useState<string>();
  useEffect(() => {
    const controller = new AbortController();
    setError('');
    api<Detail>(`/${bidderId}`, { signal: controller.signal })
      .then((value) => {
        setData(value);
        setSelected((old) =>
          value.files.some((f) => f.file_id === old)
            ? old
            : value.files[0]?.file_id || '',
        );
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      });
    return () => controller.abort();
  }, [bidderId, reload]);
  const read = useCallback(
    <T,>(path: string, init?: RequestInit) =>
      api<T>(`/${bidderId}${path}`, init),
    [bidderId],
  );
  const saveFile = useCallback(
    (path: string, filename: string) =>
      download(`/${bidderId}${path}`, filename),
    [bidderId],
  );
  const materials = data?.library?.materials || [];
  const file = data?.files.find((item) => item.file_id === selected);
  const material = materials.find((item) => item.file_id === selected);
  const relations = directRelations(materials, selected);
  const visibleFiles =
    data?.files.filter((item) => {
      const summary = materials.find((m) => m.file_id === item.file_id);
      return (
        `${item.file_name} ${item.source_path} ${summary?.bid_summary || ''} ${summary?.evidence_summary || ''}`
          .toLowerCase()
          .includes(q.toLowerCase()) &&
        (filter === 'all' ||
          (filter === 'ai' && item.provenance === 'ai_generated') ||
          (filter === 'human' && item.provenance === 'human_modified'))
      );
    }) || [];
  const fileName = (id: string) => fileLabel(id, data?.files || []);
  async function openImage(path: string) {
    const id = path.split('/')[0];
    if (!data?.files.some((f) => f.file_id === id)) {
      message.error('图片引用不属于当前资料库');
      return;
    }
    try {
      const value = await api<Preview>(`/${bidderId}/files/${id}/preview`);
      const registered = value.images.find((image) => image.path === path);
      if (
        !registered ||
        !validImageUrl(registered.url, path, {
          baseUrl: `/api/bidders/${bidderId}/files/${id}/preview`,
          version: value.parse_version,
        })
      )
        throw new Error('图片未在当前文件版本中登记');
      setZoom(registered.url);
    } catch (e) {
      message.error((e as Error).message);
    }
  }
  function beginEdit() {
    if (!material || !data?.revision) return;
    const value = {
      file_id: material.file_id,
      bid_summary: material.bid_summary,
      evidence_summary: material.evidence_summary,
      relations: material.relations.map(([a, b]) => [a, b] as [string, string]),
    };
    setDraft(value);
    setRelationKeys(value.relations.map(() => crypto.randomUUID()));
    setOriginal(value);
    setEditRevision(data.revision);
    setEditError('');
    setConflict(false);
  }
  async function saveDraft() {
    if (!draft) return;
    setSaving(true);
    try {
      const result = await editLibrary(
        bidderId,
        editRevision,
        'update_material',
        draft,
      );
      result.warning
        ? message.warning(result.warning)
        : message.success(
            result.changed ? '资料已保存，来源标记为人工修正' : '内容未变化',
          );
      setDraft(undefined);
      setReload((n) => n + 1);
    } catch (e) {
      setEditError((e as Error).message);
      setConflict(e instanceof ApiError && e.status === 409);
    } finally {
      setSaving(false);
    }
  }
  async function refreshForCompare() {
    try {
      const latest = await api<Detail>(`/${bidderId}`);
      setData(latest);
      const current = latest.library?.materials.find(
        (m) => m.file_id === draft?.file_id,
      );
      if (current && latest.revision) {
        setOriginal({
          file_id: current.file_id,
          bid_summary: current.bid_summary,
          evidence_summary: current.evidence_summary,
          relations: current.relations,
        });
        // 草稿保留；用户看过最新内容后还必须走“对比并保存”的明确确认。
        setEditRevision(latest.revision);
        setConflict(false);
        setEditError(
          '已载入最新内容作为对比基线，草稿保留；请核对后再确认保存。',
        );
      }
    } catch (e) {
      setEditError((e as Error).message);
    }
  }
  const paths = [
    ...new Set(
      (material?.evidence_summary || '').match(
        /[A-Za-z0-9_-]+\/images\/[A-Za-z0-9_.-]+/g,
      ) || [],
    ),
  ];
  return (
    <main className="business-page">
      <Heading
        title={
          <EntityLabel id={bidderId}>
            {data?.bidder.display_name || '资料库详情'}
          </EntityLabel>
        }
        description={`${data?.files.length || 0} 份来源文件 · ${data?.files.filter((f) => f.ingestion_status === 'completed').length || 0} 份已完成`}
        actions={
          <>
            <Status status={data?.bidder.status || 'processing_evidence'} />
            <Button
              disabled={!data?.editable}
              onClick={() => {
                setName(data?.bidder.display_name);
                setEditRevision(data?.revision || '');
                setEditError('');
              }}
            >
              修改名称
            </Button>
            <Link to={`/evidence-libraries/${bidderId}/progress`}>
              <Button>查看处理过程</Button>
            </Link>
            {(['json', 'md'] as const).map((format) => (
              <Button
                key={format}
                disabled={!data?.exportable}
                onClick={() =>
                  download(
                    `/${bidderId}/export?format=${format}`,
                    `${data?.bidder.display_name}_资料库.${format}`,
                  ).catch((e) => message.error(e.message))
                }
              >
                导出 {format.toUpperCase()}
              </Button>
            ))}
          </>
        }
      />
      {error && <Failure error={error} retry={() => setReload((n) => n + 1)} />}
      {!data && !error && <Spin />}
      {data?.error && (
        <Alert
          className="section-gap"
          type="error"
          title={data.error.message}
        />
      )}
      {data?.warning && (
        <Alert
          className="section-gap"
          type="warning"
          title={data.warning.message}
        />
      )}
      {data && !data.editable && (
        <Alert
          className="section-gap"
          type="info"
          title="当前资料只读"
          description="处理中、失败或无有效正式快照时不能编辑和正式导出；仍可查看已保存摘要和原件。"
        />
      )}
      {data && (
        <div className="evidence-detail-grid">
          <Card title="资料目录" className="material-directory">
            <Input.Search
              allowClear
              placeholder="搜索文件或摘要"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
            <Select
              aria-label="资料来源筛选"
              className="section-gap"
              style={{ width: '100%' }}
              value={filter}
              onChange={setFilter}
              options={[
                { label: '全部', value: 'all' },
                { label: 'AI 整理', value: 'ai' },
                { label: '人工修正', value: 'human' },
              ]}
            />
            <nav
              aria-label="资料文件目录"
              className="material-directory-scroll"
            >
              {visibleFiles.map((item) => (
                <button
                  type="button"
                  key={item.file_id}
                  aria-current={selected === item.file_id ? 'true' : undefined}
                  className={`material-file ${selected === item.file_id ? 'active' : ''}`}
                  onClick={() => setSelected(item.file_id)}
                >
                  <strong>
                    <EntityLabel id={item.file_id}>
                      {item.file_name}
                    </EntityLabel>
                  </strong>
                  <small>{item.source_path}</small>
                  <Space wrap>
                    <Provenance value={item.provenance} />
                  </Space>
                </button>
              ))}
              {visibleFiles.length === 0 && (
                <Empty description="没有符合条件的文件">
                  <Button
                    onClick={() => {
                      setFilter('all');
                      setQ('');
                    }}
                  >
                    清除筛选
                  </Button>
                </Empty>
              )}
            </nav>
          </Card>
          <section className="material-reading">
            {file ? (
              <Card
                title={
                  <Space wrap>
                    <EntityLabel id={file.file_id}>
                      {file.file_name}
                    </EntityLabel>
                    <Provenance value={file.provenance} />
                  </Space>
                }
                extra={
                  <Space wrap>
                    <Button onClick={() => setPreviewFile(selected)}>
                      查看解析原文
                    </Button>
                    <Button
                      onClick={() =>
                        saveFile(
                          `/files/${selected}/download`,
                          file.file_name,
                        ).catch((e) => message.error(e.message))
                      }
                    >
                      下载原件
                    </Button>
                  </Space>
                }
              >
                <p className="muted">
                  {file.source_path} ·{' '}
                  {parseLabels[file.parse_status] || file.parse_status}
                  {' · '}
                  {ingestionLabels[file.ingestion_status] ||
                    file.ingestion_status}
                </p>
                {material ? (
                  <>
                    <div className="material-brief">
                      <h3>简洁摘要</h3>
                      <p className="material-text">{material.bid_summary}</p>
                    </div>
                    <div className="material-section-heading">
                      <h3>详细摘要</h3>
                      <Button disabled={!data.editable} onClick={beginEdit}>
                        编辑摘要与关系
                      </Button>
                    </div>
                    <article
                      className="document-preview"
                      dangerouslySetInnerHTML={{
                        __html: safeMarkdown(material.evidence_summary, []),
                      }}
                    />
                    {paths.length > 0 && (
                      <Space wrap>
                        {paths.map((path) => (
                          <Button key={path} onClick={() => openImage(path)}>
                            查看图片 · {path}
                          </Button>
                        ))}
                      </Space>
                    )}
                    <h3>直接关联资料</h3>
                    {relations.length ? (
                      relations.map((edge) => (
                        <div
                          className="material-relation"
                          key={`${edge.from}/${edge.relation}/${edge.to}`}
                        >
                          <Button
                            type="link"
                            onClick={() => setSelected(edge.from)}
                          >
                            <EntityLabel id={edge.from}>
                              {fileName(edge.from)}
                            </EntityLabel>
                          </Button>
                          <span>→ {edge.relation} →</span>
                          <Button
                            type="link"
                            onClick={() => setSelected(edge.to)}
                          >
                            <EntityLabel id={edge.to}>
                              {fileName(edge.to)}
                            </EntityLabel>
                          </Button>
                        </div>
                      ))
                    ) : (
                      <p className="muted">
                        暂无已登记的直接关系，不自动补齐或推断关联。
                      </p>
                    )}
                  </>
                ) : (
                  <Empty
                    description={
                      data.warning?.code === 'evidence_library_unavailable'
                        ? '资料快照不可读取'
                        : '尚未整理到此文件'
                    }
                  />
                )}
              </Card>
            ) : (
              <Empty description="暂无来源文件" />
            )}
          </section>
        </div>
      )}
      <SourcePreview
        baseUrl={`/api/bidders/${bidderId}`}
        fileId={previewFile}
        close={() => setPreviewFile(undefined)}
        api={read}
        download={saveFile}
      />
      {zoom && (
        <Image
          style={{ display: 'none' }}
          src={zoom}
          alt="资料原文图片"
          preview={{
            open: true,
            onOpenChange: (open) => {
              if (!open) setZoom(undefined);
            },
          }}
        />
      )}
      <Drawer
        title="修订摘要与关系"
        rootClassName="material-editor"
        size="large"
        open={!!draft}
        onClose={() => {
          if (!saving) setDraft(undefined);
        }}
        extra={
          <Button
            type="primary"
            loading={saving}
            disabled={
              conflict ||
              !draft?.bid_summary.trim() ||
              !draft?.evidence_summary.trim() ||
              draft.relations.some(([a, b]) => !a.trim() || !b)
            }
            onClick={() =>
              modal.confirm({
                title: '确认保存本文件的改动？',
                width: 900,
                content: (
                  <div className="material-compare">
                    <div>
                      <h4>当前已保存</h4>
                      <pre>{JSON.stringify(original, null, 2)}</pre>
                    </div>
                    <div>
                      <h4>本次草稿</h4>
                      <pre>{JSON.stringify(draft, null, 2)}</pre>
                    </div>
                  </div>
                ),
                okText: '确认保存',
                onOk: saveDraft,
              })
            }
          >
            对比并保存
          </Button>
        }
      >
        <Alert
          type="info"
          title="人工修正仅表示内容被修改，不代表已核验真实有效。"
        />
        {editError && (
          <Alert
            className="section-gap"
            type={conflict ? 'error' : 'warning'}
            title={editError}
            action={
              conflict && (
                <Button onClick={refreshForCompare}>载入最新内容对比</Button>
              )
            }
          />
        )}
        {draft && (
          <div className="material-editor-sections">
            <section aria-labelledby="bid-summary-label">
              <div className="material-editor-heading">
                <label id="bid-summary-label" htmlFor="bid-summary">
                  简洁摘要
                </label>
              </div>
              <Input.TextArea
                id="bid-summary"
                rows={4}
                value={draft.bid_summary}
                onChange={(e) =>
                  setDraft({ ...draft, bid_summary: e.target.value })
                }
              />
            </section>
            <section aria-labelledby="evidence-summary-label">
              <div className="material-editor-heading">
                <label id="evidence-summary-label" htmlFor="evidence-summary">
                  详细摘要
                </label>
                <span
                  id="evidence-summary-format"
                  className="material-editor-hint"
                >
                  支持 Markdown
                </span>
              </div>
              <Input.TextArea
                id="evidence-summary"
                aria-describedby="evidence-summary-format"
                rows={12}
                value={draft.evidence_summary}
                onChange={(e) =>
                  setDraft({ ...draft, evidence_summary: e.target.value })
                }
              />
            </section>
            <section aria-labelledby="material-relations-label">
              <div className="material-editor-heading">
                <h3 id="material-relations-label">出向关系</h3>
                <span className="material-editor-hint">
                  当前文件 → 关联文件
                </span>
                <Button
                  size="small"
                  onClick={() => {
                    setRelationKeys((keys) => [...keys, crypto.randomUUID()]);
                    setDraft({
                      ...draft,
                      relations: [...draft.relations, ['', '']],
                    });
                  }}
                >
                  添加关系
                </Button>
              </div>
              {draft.relations.length === 0 && (
                <div className="material-editor-empty">
                  暂无出向关系，可按需添加。
                </div>
              )}
              {draft.relations.map(([relation, target], index) => (
                <div
                  className="material-relation-edit"
                  key={relationKeys[index]}
                >
                  <Input
                    aria-label={`关系短语 ${index + 1}`}
                    placeholder="关系短语"
                    value={relation}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        relations: draft.relations.map((pair, i) =>
                          i === index ? [e.target.value, target] : pair,
                        ),
                      })
                    }
                  />
                  <Select
                    aria-label={`目标文件 ${index + 1}`}
                    placeholder="目标文件"
                    value={target || undefined}
                    options={materials
                      .filter((m) => m.file_id !== draft.file_id)
                      .map((m) => ({
                        value: m.file_id,
                        label: fileName(m.file_id),
                      }))}
                    onChange={(value) =>
                      setDraft({
                        ...draft,
                        relations: draft.relations.map((pair, i) =>
                          i === index ? [relation, value] : pair,
                        ),
                      })
                    }
                  />
                  <Button
                    danger
                    type="text"
                    onClick={() => {
                      setRelationKeys((keys) =>
                        keys.filter((_, i) => i !== index),
                      );
                      setDraft({
                        ...draft,
                        relations: draft.relations.filter(
                          (_, i) => i !== index,
                        ),
                      });
                    }}
                  >
                    移除
                  </Button>
                </div>
              ))}
            </section>
          </div>
        )}
      </Drawer>
      <Modal
        title="修改资料库名称"
        open={name !== undefined}
        confirmLoading={saving}
        onCancel={() => setName(undefined)}
        onOk={async () => {
          if (!name?.trim()) return;
          setSaving(true);
          try {
            await editLibrary(bidderId, editRevision, 'rename', {
              display_name: name.trim(),
            });
            setName(undefined);
            setReload((n) => n + 1);
          } catch (e) {
            setEditError((e as Error).message);
          } finally {
            setSaving(false);
          }
        }}
      >
        <Input
          value={name}
          maxLength={100}
          onChange={(e) => setName(e.target.value)}
        />
        {editError && <Alert type="error" title={editError} />}
      </Modal>
    </main>
  );
}
