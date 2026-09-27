// biome-ignore-all lint/security/noDangerouslySetInnerHtml: 唯一 HTML 渲染点只接收下方 safeMarkdown 的净化产物，安全边界有专门测试。
import { useEffect, useMemo, useState } from 'react';
import { Alert, App, Button, Drawer, Input, Space, Spin, Tag } from 'antd';
import DOMPurify from 'dompurify';
import { marked } from 'marked';
import { api, download, type Preview } from '@/services/ruleLibraries';
import { Failure } from '../shared';

export function safeMarkdown(
  markdown: string,
  images: Preview['images'],
  query = '',
) {
  const html = DOMPurify.sanitize(marked.parse(markdown, { async: false }), {
    USE_PROFILES: { html: true },
    FORBID_TAGS: [
      'style',
      'iframe',
      'form',
      'input',
      'button',
      'video',
      'audio',
      'source',
    ],
    FORBID_ATTR: ['style', 'srcset'],
  });
  const dom = new DOMParser().parseFromString(html, 'text/html');
  const registered = new Map(images.map((image) => [image.path, image.url]));
  dom.querySelectorAll('img').forEach((img) => {
    const url = registered.get(img.getAttribute('src') || '');
    if (url && /^\/api\/projects\/[^/]+\/files\/[^/]+\/images\//.test(url)) {
      img.src = url;
      img.loading = 'lazy';
    } else img.replaceWith(dom.createTextNode('[未登记图片，不加载]'));
  });
  // 原文链接仅作文字展示，避免审阅时跳向外部地址或误触脚本。
  dom.querySelectorAll('a').forEach((anchor) => {
    anchor.removeAttribute('href');
    anchor.removeAttribute('target');
  });
  if (query.trim()) {
    const walker = dom.createTreeWalker(dom.body, NodeFilter.SHOW_TEXT);
    const nodes: Text[] = [];
    let node = walker.nextNode();
    while (node) {
      nodes.push(node as Text);
      node = walker.nextNode();
    }
    nodes.forEach((text) => {
      const value = text.textContent || '';
      const parts = value.split(query);
      if (parts.length < 2) return;
      const fragment = dom.createDocumentFragment();
      parts.forEach((part, i) => {
        if (i) {
          const mark = dom.createElement('mark');
          mark.textContent = query;
          fragment.append(mark);
        }
        fragment.append(dom.createTextNode(part));
      });
      text.replaceWith(fragment);
    });
  }
  return dom.body.innerHTML;
}
export default function SourcePreview({
  projectId,
  fileId,
  close,
}: {
  projectId: string;
  fileId?: string;
  close: () => void;
}) {
  const { message } = App.useApp();
  const [data, setData] = useState<Preview>();
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [reload, setReload] = useState(0);
  useEffect(() => {
    if (!fileId) return;
    const controller = new AbortController();
    setData(undefined);
    setError('');
    setQuery('');
    api<Preview>(`/${projectId}/files/${fileId}/preview`, {
      signal: controller.signal,
    })
      .then(setData)
      .catch((e) => {
        if (e.name !== 'AbortError') setError(e.message);
      });
    return () => controller.abort();
  }, [projectId, fileId, reload]);
  const html = useMemo(
    () => (data ? safeMarkdown(data.markdown, data.images, query) : ''),
    [data, query],
  );
  return (
    <Drawer
      className="source-drawer"
      title={data?.file_name || '来源文件'}
      open={!!fileId}
      onClose={close}
      size="large"
      extra={
        <Button
          onClick={() =>
            download(
              `/${projectId}/files/${fileId}/download`,
              data?.file_name || `${fileId}`,
            ).catch((e) => message.error(e.message))
          }
        >
          下载原件
        </Button>
      }
    >
      <Alert
        type="info"
        showIcon
        title="文件级来源"
        description="此处展示关联文件，不代表已精确定位到规则原句。请结合原文核对。"
      />
      <Space className="section-gap" wrap>
        <Tag>{fileId}</Tag>
        <span className="muted">{data?.source_path}</span>
      </Space>
      {error && <Failure error={error} retry={() => setReload((n) => n + 1)} />}
      {!data && !error && <Spin />}
      {data && (
        <>
          {data.warnings.map((warning) => (
            <Alert
              className="section-gap"
              key={warning}
              type="warning"
              title={warning}
            />
          ))}
          <Input.Search
            className="section-gap"
            allowClear
            placeholder="在当前文件正文中查找"
            onSearch={setQuery}
          />
          <article
            className="document-preview"
            dangerouslySetInnerHTML={{ __html: html }}
          />
        </>
      )}
    </Drawer>
  );
}
