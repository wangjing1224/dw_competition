import type { Source } from '@/services/ruleLibraries';

// 用完整快照的保存顺序编号，不能传入筛选、分页或“最近保存”的子集。
// 审核页必须传本次审核冻结的规则，不读取后来修改过的规则库。
export function ruleLabels(rules: readonly { rule_id: string }[]) {
  return new Map(
    rules.map((rule, index) => [
      rule.rule_id,
      `规则 ${String(index + 1).padStart(2, '0')}`,
    ]),
  );
}

export function fileLabel(id: string, files: readonly Source[]) {
  const file = files.find((item) => item.file_id === id);
  if (!file) return '未登记文件';
  const name = file.file_name || '未命名文件';
  const sameNames = files.filter(
    (item) => item.file_name === file.file_name && item.file_id !== id,
  );
  if (!sameNames.length) return name;
  // 重名优先用相对路径区分；连路径也相同才用真实 ID 消歧，不编造序号。
  const path = file.source_path || name;
  return sameNames.some((item) => (item.source_path || item.file_name) === path)
    ? `${path}（${id}）`
    : path;
}
