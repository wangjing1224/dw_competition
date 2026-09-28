/** 两类库共用统计排版，数字与单位作为整体换行并按基线对齐。 */
export default function LibraryCounts({
  fileCount,
  resultCount,
  resultLabel,
}: {
  fileCount: number;
  resultCount: number | null;
  resultLabel: string;
}) {
  return (
    <div className="library-numbers">
      <span className="library-metric">
        <strong>{fileCount}</strong>
        <span>份来源文件</span>
      </span>
      <span className="library-metric">
        <strong>{resultCount ?? '—'}</strong>
        <span>{resultLabel}</span>
      </span>
    </div>
  );
}
