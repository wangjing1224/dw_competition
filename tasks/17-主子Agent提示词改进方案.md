# Task 17：主子 Agent 提示词改进方案

## 1. 任务目标

本任务优先优化以下两个提示词：

- `configs/prompts/templates/bid_agent.md`
- `configs/prompts/templates/evidence_subagent.md`

同时调整一个已经由 Trace 验证不够清晰的执行边界：每个路由为 `matched` 的投标文件独立、
串行执行一次 `graph.ainvoke()`，不再把多个投标文件放入同一次主 Agent 图执行。

暂不调整投标文件摘要、资料分类、规则抽取提示词，也不重构检索和报告架构。

目标是解决真实 Trace 中已经确认的问题：

- 主 Agent 无法主动结束检索并输出最终 JSON。
- 同一事实被多轮重复检索。
- 检索结果不断累积，输入 Token 快速增长。
- 主 Agent 没有正确使用资料核验子 Agent。
- 工具参数校验失败会浪费完整轮次。
- 子 Agent 缺少明确的检索终止和结果输出约束。
- 多个投标文件共享一个根 Agent Run，Trace、步数和文件级失败边界混在一起。

## 2. Trace 依据

分析文件：

`documents/traces/new/full-trace-019efa3d-3ab6-7fa2-abe5-847260db21e6.json`

本次 Trace 的主要数据：

| 指标 | 结果 |
|---|---:|
| 相关投标文件 | 2 |
| 主模型调用 | 16 次 |
| 工具调用 | 18 次 |
| Query 总数 | 92 |
| 完全重复 Query | 9 |
| 单次最大 Query | 6 |
| 工具参数错误 | 2 次 |
| 资料子 Agent 调用 | 0 次 |
| Trace 总 Token | 约 49.8 万 |
| 最终合法审核 JSON | 0 份 |

两份相关文件都跑满了主 Agent 的 8 步上限。16 次模型响应全部以 `tool_calls` 结束，模型没有一次尝试输出最终 `AgentAuditDecision`。

第一份文件中，模型已经明确表达“没有找到具体格式文件”，后续仍继续使用更具体或近义的 Query 搜索相同事项。

工具参数说明优化后，Query 重复和超量情况有所改善，但无法解决 Agent 不停止的问题。因此下一步必须修改 Agent 自身提示词。

## 3. 根因分析

### 3.1 主 Agent 的终止条件表达错误

当前提示词：

> 信息不足时继续调用必要工具；信息充分后只返回符合下方 Schema 的 JSON。

该表述会让模型把“信息不足”持续解释为“继续搜索”，但实际审核允许以下结论：

- 投标文件未提供相关内容。
- 当前解析文本无法确认。
- 可用资料不足。
- 图片或脱敏内容无法自动核验。
- 检索一次后没有召回有效原文。

这些情况不应该无限搜索，而应该形成 `manual_review`、`non_compliant` 或其他符合业务语义的结论。

### 3.2 没有检索预算

当前提示词只限制整个 Agent 的代码步数，没有告诉模型：

- 最多允许几轮工具交互。
- 最后一轮必须输出最终结果。
- 同一事实允许检索几次。
- 工具参数错误后能否重试。

模型因此把全部 8 步都用于检索。

### 3.3 没有明确“未找到也是结果”

模型多次检索承诺函、关系说明、资质核实承诺函等内容。检索没有获得目标原文后，模型继续更换同义词搜索。

提示词需要明确：

- 一次高质量检索未找到目标内容，本身就是审核信息。
- 不得仅通过增加“全文、提交、签字、盖章”等近义词重复检索。
- 对必须提交但未找到的内容，可根据规则和现有上下文给出缺失或人工复核结论。

### 3.4 没有先进行规则分流

模型进入第一轮后立即生成大量 Query，没有先区分规则应使用哪类信息判断。

主 Agent 应先在内部将规则划分为：

1. 摘要可以直接判断。
2. 需要检索投标文件原文。
3. 需要回查招标文件原文。
4. 需要委派资料子 Agent。
5. 已知无法自动确认，直接人工复核。

该分流只用于内部决策，不增加新的输出字段。

### 3.5 没有充分信任投标文件摘要

当前两份摘要分别约 1689 字和 2491 字，已经包含：

- 明确存在的事实。
- 明确缺失的内容。
- 技术参数。
- 商务和资格信息。
- 一致性风险。

但主 Agent 仍重新检索大量摘要已经明确说明缺失的普通事项。

摘要应作为默认事实入口。只有满足以下条件时才回查投标原文：

- 规则优先级较高。
- 摘要可能遗漏原文中的决定性事实。
- 需要取得可追溯的原文切片。
- 摘要内容存在冲突或表述不明确。

### 3.6 工具选择不平衡

最新 Trace：

- `search_bid_response` 调用 12 次。
- `search_tender_source` 调用 6 次。
- `request_evidence_review` 调用 0 次。

主 Agent 没有把需要外部资料证明的规则交给资料子 Agent，而是持续在投标文件中搜索。

需要明确：

- `bid_only`：优先摘要，必要时查投标原文。
- `source_check`：规则本身不明确时才查招标原文。
- `evidence_check`：存在对应 `classified` 资料时，应优先调用资料子 Agent。
- `cross_check`：根据实际缺失信息选择投标原文、招标原文或资料核验，不要求机械调用全部工具。

### 3.7 子 Agent 也缺少明确终止规则

资料子 Agent 当前提示词只写“最多三轮”，但没有规定：

- 前两轮用于检索。
- 最后一轮必须返回 JSON。
- 空召回时立即停止。
- 同一事实不得改写 Query 反复检索。
- 哪些状态必须引用 `evidence_chunk_ids`。

虽然本次 Trace 没有真正进入子 Agent，但如果不提前修正，它可能复现主 Agent 的循环问题。

### 3.8 多文件共用一次主图执行，文件边界不够清晰

当前执行方式：

```text
一次 graph.ainvoke()
→ current_bid_index=0
→ 审核文件1
→ 清空 messages
→ current_bid_index=1
→ 审核文件2
→ 图内汇总 AuditResult
```

当前实现已通过清空 `messages` 和检索 `file_id` 过滤避免文件内容串用，但仍存在以下问题：

- 多个文件共用一个根 Trace，文件级 Token 和工具调用不便独立分析。
- `current_bid_index`、消息清空和继续路由属于手动隔离。
- 单个文件达到步数上限时，与整个任务处于同一个图执行生命周期。
- 后续如果增加文件级重试或失败降级，图内状态会更复杂。

建议改为：

```text
统一完成全部文件解析和路由
→ 汇总 matched / not_matched / uncertain
→ 对每个 matched 文件串行执行一次 graph.ainvoke()
→ 每次只传当前文件及当前摘要
→ 每个文件得到独立 FileAuditResult 和独立 Trace
→ 代码确定性汇总 AuditResult
```

第一版保持串行，不并发调用模型。原因是当前算力平台模型服务的稳定性和并发能力尚未验证，
串行执行更容易控制显存、请求速率、失败重试和 Trace 分析。

## 4. 主 Agent 提示词改进方案

### 4.1 明确审核目标

补充以下原则：

- 目标是对全部输入规则形成一次完整审核结果，不是尽可能穷举所有原文。
- 工具只用于解决会改变规则结论的关键信息缺口。
- 信息无法自动确认也是合法审核结果，必须进入最终 JSON。

### 4.2 增加内部规则分流

建议在提示词中加入：

```text
开始审核时，先在内部判断每条规则属于以下哪种处理方式：
1. 摘要可直接判断；
2. 需要投标原文；
3. 需要招标原文；
4. 需要投标方资料核验；
5. 无法自动核验，直接人工复核。

该分流不需要输出，也不要逐条解释计划。
```

改动原因：

- 避免模型一开始为全部规则生成 Query。
- 让不同 `verification` 类型自然进入正确工具。
- 减少无效检索。

预期效果：

- 第一轮工具调用只覆盖真正存在信息缺口的重点规则。
- `request_evidence_review` 能够被正常使用。

### 4.3 确立摘要优先级

建议加入：

```text
投标文件详细摘要是当前文件的默认事实入口。
摘要中已经明确存在的事实可以直接用于判断。
摘要中已经明确缺失的普通信息，不要重复检索。
只有重点规则且原文可能包含决定性事实、需要原文溯源或摘要存在冲突时，才检索投标原文。
```

改动原因：

- 当前摘要已经足够详细。
- 避免对摘要中的“明确缺失项”机械回查。

预期效果：

- 第一轮 Query 数量明显下降。
- 主 Agent 初始输入的价值被真正利用。

### 4.4 增加检索终止规则

建议加入：

```text
同一个核验事实原则上只检索一次。
检索返回空chunks、结果与目标无关或没有提供新事实时，立即停止检索该事项。
不得只通过增加“全文、提交、签字、盖章”等近义词再次查询同一事项。
未找到可靠原文时，根据规则性质输出缺失、无法确认或manual_review结论。
```

改动原因：

- 直接针对 Trace 中同义词循环搜索问题。

预期效果：

- 完全重复和语义重复 Query 大幅下降。
- 输入上下文不再持续无效增长。

### 4.5 设置工具交互预算

建议加入：

```text
每个投标文件原则上最多进行3轮工具交互。
每个工具调用必须遵守参数Schema，每次queries最多5个。
工具参数校验失败时只允许修正一次。
完成必要工具调用后必须停止检索并输出最终JSON。
不要把全部可用轮次都用于工具调用。
```

这里的“3轮”是提示词约束，不修改当前代码中的主 Agent 8步上限。代码上限继续作为异常保护。

改动原因：

- 模型需要明确的软预算。
- 8步应保留给工具调用、结果整理和格式修正，而不是全部用于搜索。

预期效果：

- 正常情况下每份文件约 2～4 次模型调用。
- 最后一轮能够输出最终 JSON。

### 4.6 达到轮次上限时强制 Agent 收敛

当前代码在主 Agent 达到步数上限后直接进入 `apply_output`。如果最后一条消息仍然是工具调用，
代码无法获得合法 `AgentAuditDecision`，只能机械地为全部规则填充“达到步数上限，需要人工
复核”的兜底文字。

该行为需要调整为：

```text
准备执行最后一轮模型调用
→ 判断当前轮次已经等于总轮次上限
→ 本轮不再给 Agent bind_tools
→ 向 Agent 注入明确的强制收敛消息
→ 要求根据已有摘要和全部工具结果输出完整 JSON
→ 只有最终输出仍不合法时，代码才使用人工复核兜底
```

这不是达到第8轮之后额外增加第9次模型调用，也不需要再设计一个独立的
`force_finalize` 图节点。Agent 节点在每次调用模型之前检查轮次：普通轮次使用
`model.bind_tools(tools)`，最后一轮直接使用原始 `model`。

建议注入的 HumanMessage：

```text
当前是第8轮，总轮次上限为8轮，你已经达到本文件的审核循环上限。
禁止继续调用任何工具。
请立即根据投标文件摘要、规则、资料目录和此前工具返回的全部有效信息，
对每条规则形成最终判断。
信息仍不足的规则使用manual_review，并在reason中说明具体缺口。
必须完整覆盖全部输入规则，只返回符合AgentAuditDecision Schema的JSON。
```

这里不应把“第8轮”写死，应由代码根据配置动态生成：

```python
HumanMessage(
    content=(
        f"当前是第{current_step}轮，总轮次上限为{max_steps}轮，"
        "你已经达到本文件的审核循环上限。禁止继续调用任何工具。"
        "请根据已有信息立即输出完整审核JSON；信息不足的规则使用manual_review。"
    )
)
```

推荐将总步数理解为“最多8次主 Agent 模型调用”，并预留最后一次用于强制输出：

```text
第1～7次：允许根据需要调用工具
第8次：只允许输出最终JSON，不再暴露工具
```

伪代码：

```python
async def bid_agent_node(state, model, tools, max_steps):
    current_step = state["react_step_count"] + 1
    is_final_step = current_step >= max_steps
    messages = build_bid_agent_messages(state)

    if is_final_step:
        messages.append(
            HumanMessage(
                content=build_force_finalize_message(
                    current_step=current_step,
                    max_steps=max_steps,
                )
            )
        )
        # 最后一轮直接调用模型，不再把工具Schema传给模型。
        response = await model.ainvoke(messages)
    else:
        response = await model.bind_tools(tools).ainvoke(messages)

    return {
        "messages": [response],
        "react_step_count": current_step,
    }
```

路由逻辑只需保证：

```python
if valid_final_json(last_message):
    return "apply_output"
if last_message.tool_calls:
    return "tools"
return "retry_format"
```

由于最后一轮没有向模型暴露工具，正常情况下不会再产生 `tool_calls`。如果最后一轮仍未输出合法
JSON，才进入格式处理或人工兜底。

改动原因：

- 轮次上限的目的应是促使 Agent 收敛，而不是直接放弃 Agent 已经获得的信息。
- 前面多轮检索结果可能已经足以判断大部分规则，全部机械转人工会浪费已有结果。
- 禁用工具后，模型无法继续循环，只能形成最终结果。

预期效果：

- 即使 Agent 没有主动停止，也会在最后一轮尝试输出完整报告。
- 已经获取证据的规则可以正常形成结论。
- 只有确实无法判断的规则进入 `manual_review`。
- “全部规则因达到步数上限而人工复核”的情况显著减少。

### 4.7 明确资料核验选择

建议加入：

```text
规则需要资料核验时，先查看可用资料目录：
- 存在对应evidence_type且至少一个文件为classified：调用request_evidence_review；
- 只有manual_review文件：不调用子Agent，直接标记人工复核并说明资料不可自动读取；
- 不存在对应资料：不调用子Agent，说明缺少核验资料。

不要用search_bid_response替代本应由资料子Agent完成的资料真实性或证明材料核验。
```

改动原因：

- 当前主 Agent 完全没有调用子 Agent。
- 投标文件声明不能替代营业执照、合同、证书等资料原文核验。

预期效果：

- `evidence_check` 规则能产生真实资料核验结果。
- 减少在投标文件中搜索资料证明的无效行为。

### 4.8 强制最终输出

建议替换当前结尾：

```text
工具用于补充必要事实，不要求消除所有不确定性。
达到工具预算、检索无新增信息或剩余问题无法自动核验时，必须停止调用工具。
随后只返回符合下方Schema的完整JSON。
全部输入规则必须且只能出现一次；无法确认的规则使用manual_review，不得遗漏。
```

改动原因：

- 当前16次模型响应全部选择工具调用，没有尝试输出 JSON。

预期效果：

- 主 Agent 能够在存在不确定信息时正常结束。
- 避免代码使用整份文件人工复核兜底。

## 5. 资料子 Agent 提示词改进方案

### 5.1 明确核验边界

建议加入：

```text
你只核验verification_request指定的事实，不扩展为整条规则或整份资料审核。
优先使用资料摘要确定最可能包含目标事实的文件。
```

改动原因：

- 防止子 Agent 自行扩大任务范围。

预期效果：

- Query 更集中。
- 子 Agent 输出保持精简。

### 5.2 限制检索轮次

建议将“最多三轮”细化为：

```text
最多进行2轮资料检索，第3次模型调用必须返回最终JSON，不得再调用工具。
通常应在一次工具调用中提交1至5个高辨识度query。
```

改动原因：

- “最多三轮”没有说明最后一轮用途。

预期效果：

- 子 Agent 能稳定在三次模型调用内结束。

子 Agent 也应采用与主 Agent 相同的强制收敛机制：

```text
第1～2次：允许调用search_evidence
第3次：注入“已达到3轮上限”的HumanMessage，禁用工具并强制输出EvidenceReviewDecision
```

若第3次仍不能输出合法 JSON，代码再生成 `unverifiable` 兜底结果。不能在第3次模型仍想调用
工具时直接跳过最终输出机会。

### 5.3 增加空结果处理

建议加入：

```text
检索返回空chunks或没有支持目标事实的原文时，不得改写同义词循环搜索。
若仍有一次必要检索机会，可更换检索对象或指定file_ids；否则立即输出unverifiable。
```

改动原因：

- 防止复现主 Agent 的无限同义词搜索。

预期效果：

- 无证据时快速结束。

### 5.4 严格约束证据引用

建议加入：

```text
supported、partially_supported和contradicted必须引用至少一个实际返回的evidence_chunk_ids。
ocr_required、unsupported和unverifiable可以不引用chunk，但summary必须说明具体缺口。
不得把资料摘要、文件名或未返回的内容当作证据。
```

改动原因：

- 与现有代码校验保持一致。
- 避免模型给出无来源的自动核验结论。

预期效果：

- 子 Agent 返回结果可直接供主 Agent 和报告使用。

## 6. 建议后的主 Agent 提示词结构

```text
# 角色与目标

# 输入信息的可信层级
- 规则
- 当前投标文件摘要
- 资料目录
- 工具返回原文

# 内部规则分流

# 工具选择规则
- 投标原文
- 招标原文
- 资料核验

# 工具预算与终止条件

# 结论规则
- compliant
- non_compliant
- manual_review
- not_applicable

# 最终JSON要求

# 输出Schema
```

## 7. 建议后的子 Agent 提示词结构

```text
# 角色与边界

# 输入
- verification_request
- evidence_type
- 可用资料摘要

# 检索策略

# 轮次与终止条件

# 状态判定

# 证据引用要求

# 最终JSON要求

# 输出Schema
```

## 8. 本任务计划修改文件

```text
configs/prompts/templates/
├── bid_agent.md               # 主 Agent 决策、工具预算和终止条件
└── evidence_subagent.md       # 子 Agent 检索边界、轮次和证据要求

app/graph/
├── audit_task_graph.py         # 每个 matched 文件独立串行 ainvoke
├── audit_state.py              # 每次状态只包含当前投标文件
├── state_updates.py            # 文件完成和最终结果处理
├── evidence_graph.py           # 子 Agent 文件内短循环
└── routing.py                  # 文件结束路由

app/agents/
├── bid_agent.py                # 最后一轮不bind_tools并注入强制输出提示
└── evidence_subagent.py        # 第三轮不bind_tools并注入强制输出提示
```

本任务原则上不修改：

- `app/tools/`
- `configs/prompts/templates/bid_document_summary*.md`
- `configs/prompts/templates/evidence_classification*.md`
- `configs/prompts/templates/rule_extraction*.md`

如果提示词优化后仍出现工具参数超过 Schema 上限，再单独评估工具层是否需要自动截断保护。本任务不提前增加该逻辑。

## 9. 多文件独立执行改造

### 9.1 路由阶段保持不变

上传测试文件夹后，仍然：

1. 保存和解析全部文件。
2. 每个文件独立调用一次摘要/路由 LLM。
3. 汇总 `matched`、`not_matched` 和 `uncertain`。
4. 只为全部 `matched` 文件统一构建一次任务级投标索引。

路由模型不会一次读取多个文件全文，也不进入主 Agent 图。

### 9.2 每个 matched 文件独立 invoke

伪代码：

```python
file_results = list(context.routed_file_results)

for bid_file in context.bid_files:
    file_context = build_single_file_context(
        context=context,
        bid_file=bid_file,
        bid_summary=context.bid_summaries[bid_file.file_id],
    )
    final_state = await graph.ainvoke(
        build_initial_audit_state(file_context),
        config=build_file_trace_config(context, bid_file),
    )
    file_results.append(require_file_result(final_state))

return build_audit_result(
    context=context,
    file_results=file_results,
)
```

每次调用只包含：

- 当前投标文件。
- 当前投标文件摘要。
- 公共规则。
- 公共投标方资料目录。
- 当前任务和投标方身份。

### 9.3 Trace 标识

每个文件使用独立 `thread_id` 和元数据：

```python
thread_id = f"{task_id}-{file_id}-{unique_suffix}"
metadata = {
    "task_id": task_id,
    "file_id": file_id,
    "file_name": file_name,
    "project_id": project_id,
    "bidder_id": bidder_id,
}
```

预期 LangSmith 结构：

```text
audit-2 / file-42 → 独立 Thread / Trace
audit-2 / file-43 → 独立 Thread / Trace
```

### 9.4 失败隔离

单个文件没有形成合法 Agent 结果时：

- 仅该文件生成全规则 `manual_review` 兜底结果。
- 继续串行审核后续 matched 文件。
- 整个任务仍可生成包含各文件结果的 JSON 和 Word。

如果出现模型连接失败、索引损坏等任务级基础设施异常，仍允许后台任务整体失败，不在本任务中
增加复杂重试队列。

### 9.5 不变部分

- 投标文件摘要和路由逻辑不变。
- 任务级投标索引不重复构建。
- `search_bid_response` 继续通过当前 `file_id` 过滤。
- 规则和资料目录作为只读公共输入复用。
- 最终仍输出一份任务级 JSON。
- Word 仍根据 JSON 中每个 `file_result` 分别生成。

## 10. 验证步骤

### 10.1 主 Agent 验证

继续使用当前相同的数据和模型运行一次审核，下载完整 Trace。

重点检查：

- 每份文件是否在最多3轮工具交互后停止。
- 是否仍存在相同事项的同义词重复搜索。
- 是否有单次超过5个 Query。
- 是否调用 `request_evidence_review`。
- 最后一轮是否输出合法 `AgentAuditDecision`。
- 达到第8轮时是否注入动态轮次提示并禁用全部工具。
- 强制收敛失败后才允许进入全规则人工复核兜底。
- 两份相关文件是否不再全部走人工兜底。

### 10.2 子 Agent 验证

从 Trace 中检查每次资料核验：

- 是否只检索指定 `evidence_type`。
- 是否优先使用摘要指向的 `file_ids`。
- 是否最多2轮检索。
- 第3次模型调用是否禁用工具并强制输出最终 JSON。
- 是否引用真实 `evidence_chunk_ids`。
- 空结果是否快速返回 `unverifiable`。

### 10.3 多文件执行验证

- 两个 matched 文件应产生两个独立根 Trace。
- 每个 Trace 的 HumanMessage 只包含当前文件摘要。
- 每个 Trace 的投标原文检索结果只包含当前 `file_id`。
- 文件1达到步数上限时，文件2仍能继续独立执行。
- 最终任务级 JSON 仍包含全部路由结果和审核结果。

## 11. 预期指标

| 指标 | 当前 | 目标 |
|---|---:|---:|
| 每份文件主模型调用 | 8次 | 2～4次 |
| 每份文件工具交互 | 7轮 | 0～3轮 |
| 两份文件 Query 总数 | 92 | 不高于30 |
| 工具参数错误 | 2次 | 0次 |
| 资料子 Agent 调用 | 0次 | 按实际规则触发 |
| 最终合法 JSON | 0份 | 每个相关文件1份 |
| 根 Agent Trace | 1条包含2个文件 | 每个matched文件1条 |
| Trace 总 Token | 约49.8万 | 显著下降 |

## 12. 完成标准

- 主 Agent 不再把“信息不足”自动等同于“继续检索”。
- 主 Agent 能在存在不确定项时正常输出完整 JSON。
- 达到轮次上限时先强制 Agent 输出报告，而不是直接机械填充兜底文字。
- 同一事实不会通过近义 Query 多轮重复查询。
- `evidence_check` 规则能够按资料目录调用子 Agent。
- 子 Agent 在有限轮次内返回带有效来源的核验结果。
- 每个 matched 文件拥有独立 Agent State、步数和 Trace。
- matched 文件按顺序串行审核，不并发占用模型服务。
- 单个文件的审核兜底结果不影响后续文件继续执行。
- 全部规则仍被完整覆盖，不因减少工具调用而遗漏。
- JSON 输出结构保持不变，现有报告生成代码无需同步重构。
