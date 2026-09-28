/** 同源业务接口。错误保留状态码，409 编辑冲突由页面处理而非自动重试。 */
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}
export async function requestJSON<T>(
  url: string,
  init: RequestInit = {},
): Promise<T> {
  const timeout = AbortSignal.timeout(
    init.body instanceof FormData ? 300000 : 30000,
  );
  const response = await fetch(url, {
    ...init,
    signal: init.signal ? AbortSignal.any([init.signal, timeout]) : timeout,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(
      response.status,
      body.error?.code || 'request_failed',
      body.error?.message || `请求失败 (${response.status})`,
    );
  }
  return response.json();
}
export async function downloadFile(urlPath: string, filename: string) {
  const response = await fetch(urlPath);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(
      response.status,
      body.error?.code || 'download_failed',
      body.error?.message || '下载失败',
    );
  }
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
