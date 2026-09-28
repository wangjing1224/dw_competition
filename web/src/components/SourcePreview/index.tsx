// biome-ignore-all lint/security/noDangerouslySetInnerHtml: 唯一 HTML 渲染点只接收下方 safeMarkdown 的净化产物，安全边界有专门测试。

import {
  Alert,
  App,
  Button,
  Drawer,
  Image,
  Input,
  Space,
  Spin,
  Tag,
} from 'antd';
import DOMPurify from 'dompurify';
import { marked } from 'marked';
import { useEffect, useMemo, useState } from 'react';
import { Failure } from '@/pages/RuleLibraries/shared';
import type { Preview } from '@/services/ruleLibraries';

export function validImageUrl(
  url: string,
  path: string,
  scope?: { baseUrl: string; version: string },
) {
  // 必须为登记的同源逻辑路径；额外校验业务文件、图片名称和快照版本。
  const match = path.match(
    /^(file-[A-Za-z0-9_-]+)\/images\/([A-Za-z0-9_-]+\.(?:png|jpe?g))$/,
  );
  if (
    !match ||
    !url.startsWith('/api/') ||
    url.startsWith('//') ||
    url.includes('\\')
  )
    return false;
  const parsed = new URL(url, window.location.origin);
  if (
    !/^\/api\/(?:projects|bidders)\/[^/]+\/files\/[^/]+\/preview$/.test(
      parsed.pathname,
    )
  )
    return false;
  if (
    parsed.pathname.split('/')[5] !== match[1] ||
    parsed.searchParams.get('image_name') !== match[2]
  )
    return false;
  const version = parsed.searchParams.get('version') || '';
  return (
    /^[a-f0-9]{64}$/.test(version) &&
    [...parsed.searchParams.keys()].length === 2 &&
    (!scope || (parsed.pathname === scope.baseUrl && version === scope.version))
  );
}

export function safeMarkdown(
  markdown: string,
  images: Preview['images'],
  query = '',
  scope?: { baseUrl: string; version: string },
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
    if (url && validImageUrl(url, img.getAttribute('src') || '', scope)) {
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
  baseUrl,
  fileId,
  api,
  download,
  close,
}: {
  baseUrl: string;
  fileId?: string;
  api: <T>(path: string, init?: RequestInit) => Promise<T>;
  download: (path: string, filename: string) => Promise<void>;
  close: () => void;
}) {
  const { message } = App.useApp();
  const [data, setData] = useState<Preview>();
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [reload, setReload] = useState(0);
  const [zoom, setZoom] = useState<string>();
  useEffect(() => {
    if (!fileId) return;
    const controller = new AbortController();
    setData(undefined);
    setError('');
    setQuery('');
    api<Preview>(`/files/${fileId}/preview`, {
      signal: controller.signal,
    })
      .then(setData)
      .catch((e) => {
        if (e.name !== 'AbortError') setError(e.message);
      });
    return () => controller.abort();
  }, [api, fileId, reload]);
  const html = useMemo(
    () =>
      data
        ? safeMarkdown(data.markdown, data.images, query, {
            baseUrl: `${baseUrl}/files/${fileId}/preview`,
            version: data.parse_version,
          })
        : '',
    [data, query, baseUrl, fileId],
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
              `/files/${fileId}/download`,
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
        description="展示登记文件的解析正文；不代表已精确定位原句，请结合原件核对。图片可点击放大。"
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
            onErrorCapture={(event) => {
              const target = event.target;
              if (target instanceof HTMLImageElement) {
                const notice = document.createElement('span');
                notice.textContent = '图片加载失败，请重试预览或下载原件。';
                target.replaceWith(notice);
              }
            }}
            onClick={(event) => {
              const target = event.target;
              if (
                target instanceof HTMLImageElement &&
                data.images.some(
                  (image) => image.url === target.getAttribute('src'),
                )
              )
                setZoom(target.src);
            }}
            dangerouslySetInnerHTML={{ __html: html }}
          />
        </>
      )}
      {zoom && (
        <Image
          style={{ display: 'none' }}
          src={zoom}
          alt="原文图片"
          preview={{
            open: true,
            onOpenChange: (open) => {
              if (!open) setZoom(undefined);
            },
          }}
        />
      )}
    </Drawer>
  );
}
