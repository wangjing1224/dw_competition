import {
  ArrowRightOutlined,
  FolderOpenOutlined,
  PlusOutlined,
} from '@ant-design/icons';
import { Link } from '@umijs/max';
import {
  Button,
  Card,
  Empty,
  Input,
  Pagination,
  Segmented,
  Space,
  Spin,
  Tag,
} from 'antd';
import { useEffect, useState } from 'react';
import {
  dateText,
  type ListResult,
  listLibraries,
  processing,
} from '@/services/evidenceLibraries';
import { Failure, Heading, Status } from '../shared';

export default function Libraries() {
  const [data, setData] = useState<ListResult>();
  const [q, setQ] = useState('');
  const [status, setStatus] = useState('');
  const [attention, setAttention] = useState(false);
  const [page, setPage] = useState(1);
  const [reload, setReload] = useState(0);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    setLoading(true);
    async function load() {
      try {
        const result = await listLibraries(
          { page, q, status, attention_only: attention },
          controller.signal,
        );
        if (controller.signal.aborted) return;
        setData(result);
        setError('');
        if (result.total && page > Math.ceil(result.total / result.page_size))
          setPage(1);
        if (result.overview.processing_count)
          timer = setTimeout(() => {
            if (!document.hidden) void load();
            else timer = setTimeout(load, 6000);
          }, 6000);
      } catch (e) {
        if (!controller.signal.aborted) setError((e as Error).message);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }
    void load();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [page, q, status, attention, reload]);
  const cards = data
    ? ([
        ['资料库总数', data.overview.library_count],
        ['可供审核选择', data.overview.ready_count],
        ['正在处理', data.overview.processing_count],
        ['需关注', data.overview.attention_count],
      ] as const)
    : [];
  const first =
    data?.overview.library_count === 0 && !q && !status && !attention;
  return (
    <main className="business-page library-list-page">
      <Heading
        backTo="/home"
        backLabel="返回工作台"
        title="投标方资料库"
        description="独立整理投标方证明材料，按文件查看事实、关联与来源。"
        actions={
          <Link to="/evidence-libraries/new">
            <Button type="primary" icon={<PlusOutlined />}>
              新建资料库
            </Button>
          </Link>
        }
      />
      <div className="stat-grid">
        {cards.map(([label, value], i) => (
          <button
            type="button"
            className={`stat-card stat-${i}`}
            key={label}
            disabled={i === 1}
            onClick={() => {
              setPage(1);
              setQ('');
              setStatus(i === 2 ? 'processing_evidence' : '');
              setAttention(i === 3);
            }}
          >
            <span>{label}</span>
            <strong>{value}</strong>
            <span className="stat-foot">
              {i === 1
                ? '摘要与原文索引均已就绪'
                : i === 3
                  ? '失败或数据异常 · 全库统计'
                  : '全部资料库统计'}
            </span>
          </button>
        ))}
      </div>
      <div className="list-toolbar">
        <Segmented
          value={status}
          options={[
            { label: '全部', value: '' },
            { label: '已完成', value: 'evidence_ready' },
            { label: '处理中', value: 'processing_evidence' },
            { label: '失败', value: 'evidence_failed' },
          ]}
          onChange={(value) => {
            setStatus(String(value));
            setPage(1);
          }}
        />
        <Space>
          {attention && (
            <Tag
              closable
              onClose={() => {
                setAttention(false);
                setPage(1);
              }}
            >
              只看需关注
            </Tag>
          )}
          <Input.Search
            allowClear
            maxLength={100}
            placeholder="搜索资料库名称或 ID"
            onSearch={(value) => {
              setQ(value);
              setPage(1);
            }}
          />
        </Space>
      </div>
      {error && <Failure error={error} retry={() => setReload((n) => n + 1)} />}
      <Spin spinning={loading} wrapperClassName="library-results">
        <div
          className={`library-grid${data?.items.length === 0 ? ' library-grid-empty' : ''}`}
        >
          {data?.items.map((item) => (
            <Card className="library-card" key={item.bidder_id}>
              <div className="library-top">
                <span className="module-icon small">
                  <FolderOpenOutlined />
                </span>
                <Status status={item.status} />
              </div>
              <h2>{item.display_name}</h2>
              <p className="muted">{item.bidder_id}</p>
              <div className="library-numbers">
                <span>{item.file_count} 份来源文件</span>
                <strong>{item.material_count ?? '—'}</strong>
                <span>份已整理摘要</span>
              </div>
              <Space wrap>
                {item.has_reading_limit && <Tag color="orange">含读取限制</Tag>}
                {item.available_for_audit && (
                  <Tag color="green">可供审核选择</Tag>
                )}
              </Space>
              {item.warning && <p className="muted">{item.warning.message}</p>}
              <div className="library-footer">
                <small>{dateText(item.updated_at)}</small>
                <Link
                  to={`/evidence-libraries/${item.bidder_id}${processing(item.status) ? '/progress' : ''}`}
                >
                  {processing(item.status) ? '查看进度' : '查看详情'}{' '}
                  <ArrowRightOutlined />
                </Link>
              </div>
            </Card>
          ))}
          {!error && data?.items.length === 0 && (
            <Empty
              className="library-empty"
              description={
                first
                  ? '尚未创建资料库'
                  : status === 'processing_evidence'
                    ? '暂无正在处理的资料库'
                    : status === 'evidence_failed'
                      ? '暂无处理失败的资料库'
                      : '暂无符合条件的资料库'
              }
            >
              {first && (
                <Link to="/evidence-libraries/new">
                  <Button type="primary">新建资料库</Button>
                </Link>
              )}
            </Empty>
          )}
        </div>
      </Spin>
      <nav className="list-pagination" aria-label="资料库分页">
        <span>共 {data?.total || 0} 个资料库</span>
        <div
          className="list-pagination-controls"
          aria-hidden={data?.total === 0}
          inert={data?.total === 0}
        >
          <Pagination
            current={page}
            pageSize={data?.page_size || 12}
            total={data?.total || 0}
            onChange={setPage}
            showSizeChanger={false}
            disabled={data?.total === 0}
          />
        </div>
      </nav>
    </main>
  );
}
