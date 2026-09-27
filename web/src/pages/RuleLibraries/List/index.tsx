import { useEffect, useState } from 'react';
import { Link } from '@umijs/max';
import {
  Alert,
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
import {
  ArrowRightOutlined,
  PlusOutlined,
  ReadOutlined,
  FileTextOutlined,
} from '@ant-design/icons';
import {
  categories,
  dateText,
  listLibraries,
  api,
  phases,
  type ListResult,
  type ProgressData,
} from '@/services/ruleLibraries';
import { Failure, PageHeading, Status } from '../shared';

export default function Libraries() {
  const [data, setData] = useState<ListResult>();
  const [error, setError] = useState('');
  const [q, setQ] = useState('');
  const [status, setStatus] = useState('');
  const [attention, setAttention] = useState(false);
  const [page, setPage] = useState(1);
  const [reload, setReload] = useState(0);
  const [loading, setLoading] = useState(true);
  const [recent, setRecent] = useState<ProgressData>();
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError('');
    listLibraries(
      { page, q, status, attention_only: attention },
      controller.signal,
    )
      .then(setData)
      .catch((e) => {
        if (e.name !== 'AbortError') setError(e.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [page, q, status, attention, reload]);
  useEffect(() => {
    if (!data?.overview.processing_count) {
      setRecent(undefined);
      return;
    }
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        if (!document.hidden) {
          const latest = await listLibraries(
            { status: 'processing_rules', page: 1, page_size: 1 },
            controller.signal,
          );
          if (latest.items[0]) {
            setRecent(
              await api<ProgressData>(
                `/${latest.items[0].project_id}/progress`,
                { signal: controller.signal },
              ),
            );
          } else {
            setRecent(undefined);
            setReload((n) => n + 1);
            return;
          }
        }
      } catch {
        /* 列表仍可使用，不把网络错误伪装成任务失败。 */
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, 6000);
    }
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [data?.overview.processing_count]);
  const cards =
    data &&
    ([
      ['规则库总数', data.overview.project_count],
      ['可供审核选择', data.overview.ready_count],
      ['正在处理', data.overview.processing_count],
      ['需关注', data.overview.attention_count],
    ] as const);
  return (
    <main className="business-page">
      <PageHeading
        back={false}
        title="规则库"
        description="独立管理招标要求，为投标文件审核提供统一依据。"
        actions={
          <Link to="/rule-libraries/new">
            <Button type="primary" icon={<PlusOutlined />}>
              新建规则库
            </Button>
          </Link>
        }
      />
      <div className="stat-grid">
        {cards?.map(([label, value], i) => (
          <button
            type="button"
            className={`stat-card stat-${i}`}
            key={label}
            disabled={i === 1 || (i === 3 && value === 0)}
            onClick={() => {
              setPage(1);
              setQ('');
              setAttention(i === 3);
              setStatus(i === 2 ? 'processing_rules' : '');
            }}
          >
            <span>{label}</span>
            <strong>{value}</strong>
            <span className="stat-foot">
              {i === 1
                ? '有效规则与原文索引均已就绪'
                : i === 3
                  ? value === 0
                    ? '暂无异常 · 全库统计'
                    : '失败或数据异常 · 全库统计'
                  : '全部规则库统计'}
            </span>
          </button>
        ))}
      </div>
      {recent && (
        <Alert
          className="section-gap"
          type="info"
          showIcon
          title={
            <Space wrap>
              <strong>{recent.display_name}</strong>
              <span>
                {phases[recent.phase]} · 已完成{' '}
                {recent.completed_file_ids.length}/{recent.document_count}{' '}
                份文件 · 已保存 {recent.saved_rule_count} 条规则
              </span>
              <Link to={`/rule-libraries/${recent.project_id}/progress`}>
                查看执行轨迹 <ArrowRightOutlined />
              </Link>
            </Space>
          }
        />
      )}
      <div className="list-toolbar">
        <Segmented
          value={status}
          options={[
            { label: '全部', value: '' },
            { label: '已完成', value: 'rule_ready' },
            { label: '处理中', value: 'processing_rules' },
            { label: '失败', value: 'rule_failed' },
          ]}
          onChange={(v) => {
            setStatus(String(v));
            setPage(1);
          }}
        />
        <Space>
          {attention && (
            <Tag closable onClose={() => setAttention(false)}>
              只看需关注
            </Tag>
          )}
          <Input.Search
            allowClear
            placeholder="搜索规则库名称或 ID"
            onSearch={(v) => {
              setQ(v);
              setPage(1);
            }}
            style={{ width: 280 }}
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </Space>
      </div>
      {error && <Failure error={error} retry={() => setReload((n) => n + 1)} />}
      <Spin spinning={loading}>
        <div className="library-grid">
          {data?.items.map((item) => (
            <Card className="library-card" key={item.project_id}>
              <div className="library-top">
                <span className="module-icon small">
                  <ReadOutlined />
                </span>
                <Status status={item.status} />
              </div>
              <h2>{item.display_name}</h2>
              <div className="muted">{item.project_id}</div>
              <div className="library-numbers">
                <span>
                  <FileTextOutlined /> {item.file_count} 份来源文件
                </span>
                <span>
                  <strong>{item.rule_count ?? '—'}</strong> 条规则
                </span>
              </div>
              <div className="category-chips">
                {item.category_counts &&
                  Object.entries(item.category_counts)
                    .filter(([, count]) => count > 0)
                    .map(([key, count]) => (
                      <Tag key={key}>
                        {categories[key]} {count}
                      </Tag>
                    ))}
              </div>
              {item.warning && (
                <p className="warning-text">{item.warning.message}</p>
              )}
              {item.rule_count === 0 && item.status === 'rule_ready' && (
                <p className="warning-text">处理完成，但未提取到规则</p>
              )}
              <div className="library-footer">
                <span>{dateText(item.updated_at)}</span>
                <Link
                  to={`/rule-libraries/${item.project_id}${item.status === 'processing_rules' ? '/progress' : ''}`}
                >
                  {item.status === 'processing_rules' ? '查看进度' : '查看详情'}{' '}
                  <ArrowRightOutlined />
                </Link>
              </div>
            </Card>
          ))}
        </div>
        {!loading && !error && data?.items.length === 0 && (
          <Empty description="暂无符合条件的规则库">
            <Link to="/rule-libraries/new">
              <Button type="primary">新建规则库</Button>
            </Link>
          </Empty>
        )}
      </Spin>
      <Pagination
        className="list-pagination"
        current={page}
        total={data?.total || 0}
        pageSize={12}
        showSizeChanger={false}
        onChange={setPage}
        showTotal={(total) => `共 ${total} 个规则库`}
      />
    </main>
  );
}
