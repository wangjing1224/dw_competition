import { useCallback } from 'react';
import Preview from '@/components/SourcePreview';
import { api, download } from '@/services/ruleLibraries';

export { safeMarkdown } from '@/components/SourcePreview';

export default function SourcePreview({
  projectId,
  fileId,
  close,
}: {
  projectId: string;
  fileId?: string;
  close: () => void;
}) {
  const read = useCallback(
    <T,>(path: string, init?: RequestInit) =>
      api<T>(`/${projectId}${path}`, init),
    [projectId],
  );
  const save = useCallback(
    (path: string, name: string) => download(`/${projectId}${path}`, name),
    [projectId],
  );
  return (
    <Preview
      baseUrl={`/api/projects/${projectId}`}
      fileId={fileId}
      close={close}
      api={read}
      download={save}
    />
  );
}
