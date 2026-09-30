import { describe, expect, it } from 'vitest';
import { fileLabel, ruleLabels } from './displayLabels';

describe('业务展示标签不改变身份', () => {
  it('规则从完整快照连续编号，不使用全局 ID 后缀或筛选后的下标', () => {
    const rules = ['rule-147', 'rule-193', 'rule-205'].map((rule_id) => ({
      rule_id,
    }));
    const labels = ruleLabels(rules);
    expect([...labels.values()]).toEqual(['规则 01', '规则 02', '规则 03']);
    expect(
      rules
        .slice(1)
        .reverse()
        .map((r) => labels.get(r.rule_id)),
    ).toEqual(['规则 03', '规则 02']);
    expect(ruleLabels([{ rule_id: 'rule-888' }]).get('rule-888')).toBe(
      '规则 01',
    );
    expect(labels.get('rule-147')).toBe('规则 01');
    expect(rules[0].rule_id).toBe('rule-147');
  });

  it('普通文件显示名称，重名显示路径，相同路径才保留 ID 消歧', () => {
    const files = [
      { file_id: 'file-18', file_name: '合同.pdf', source_path: '甲/合同.pdf' },
      { file_id: 'file-26', file_name: '合同.pdf', source_path: '乙/合同.pdf' },
      { file_id: 'file-27', file_name: '验收.pdf', source_path: '乙/验收.pdf' },
    ];
    expect(fileLabel('file-18', files)).toBe('甲/合同.pdf');
    expect(fileLabel('file-26', files)).toBe('乙/合同.pdf');
    expect(fileLabel('file-27', files)).toBe('验收.pdf');
    const duplicates = [...files, { ...files[0], file_id: 'file-30' }];
    expect(fileLabel('file-18', duplicates)).toBe('甲/合同.pdf（file-18）');
    expect(fileLabel('file-30', duplicates)).toBe('甲/合同.pdf（file-30）');
    expect(fileLabel('file-404', files)).toBe('未登记文件');
  });
});
