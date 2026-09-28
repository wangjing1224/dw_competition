const extensions = new Set([
  'doc',
  'docx',
  'pdf',
  'xlsx',
  'txt',
  'md',
  'png',
  'jpg',
  'jpeg',
]);
export type PendingFile = { path: string; file: File };
export function validateFiles(files: File[], existing: PendingFile[]) {
  const known = new Set(existing.map((f) => f.path));
  const accepted: PendingFile[] = [];
  const errors: string[] = [];
  files.forEach((file) => {
    const path = file.webkitRelativePath || file.name;
    if (!extensions.has(file.name.split('.').pop()?.toLowerCase() || ''))
      errors.push(`${path}：不支持此格式`);
    else if (!file.size) errors.push(`${path}：空文件`);
    else if (file.size > 100 * 1024 * 1024)
      errors.push(`${path}：超过 100 MiB`);
    else if (known.has(path)) errors.push(`${path}：重复路径`);
    else {
      accepted.push({ path, file });
      known.add(path);
    }
  });
  return { accepted, errors };
}
