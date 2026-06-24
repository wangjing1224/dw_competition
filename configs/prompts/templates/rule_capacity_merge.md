# 规则容量合并

当前规则数量超过 ${maximum} 条。你只能把审核目标相同或高度重叠的规则合并，不能新增规则，
不能仅为了压缩数量而删除独立的重要要求。

每个操作必须为 `merge`，`rule_id` 引用被保留的目标规则，`merged_rule_ids` 列出被吸收并删除的
其他规则 ID，并在 `requirement` 中给出合并后的紧凑规则。
合并后的规则应保留更高的重要程度、更深的核验方式以及所需资料类型。

只返回符合下方 Schema 的 JSON，`project_profile` 使用空对象，不输出解释。

${rule_delta_schema}
