# Task 18：新 Trace 提示词收敛与 Word 报告重构

## 1. 任务目标

Task17 已经解决以下问题：

- 每个匹配的投标文件独立、串行运行一次主 Agent。
- 主 Agent 和资料子 Agent 最后一轮不再绑定工具，并强制输出合法 JSON。
- 主 Agent 能够实际调用资料核验子 Agent。
- 工具参数符合 Schema，没有再出现参数校验错误。
- 两个投标文件均生成了完整的 28 条规则审核结果。

本任务不再重构 Agent 总体架构，重点解决 Task17 之后真实 Trace 暴露的剩余问题：

1. 主 Agent 虽然能完成审核，但仍然跑满 8 轮。
2. 当前检索工具只接受扁平 Query 列表，模型为了覆盖不同原文位置而多轮检索同一审核问题。
3. 主 Agent 没有严格根据资料目录决定是否调用资料子 Agent。
4. 一条规则可能需要多类资料，但当前资料核验工具一次只能表达一个 `evidence_type`，模型只能
   重复调用工具或把不同资料类型混入同一个任务。
5. “当前类别未召回”被部分结论扩大解释为“投标人未提供”。
6. 规则抽取批次较小且规则准入提示仍不够严格，产生大量细碎规则；确定性压缩随后把同类规则
   直接拼接，形成超长规则。
7. 工具结果不断累积，使每轮主模型输入持续增长。
8. Word 报告直接输出 Markdown 和英文枚举，超长规则被放入窄表格，无法正常阅读。

目标不是继续增加新的流程节点，而是让现有流程：

- 更早停止。
- 一次工具调用覆盖多个独立审核问题，并保证每个问题都有自己的召回结果。
- 一次主 Agent 资料委派可以包含多个不同资料类别，但每个子任务仍保持单一事实、单一类别。
- 更准确地区分“明确缺失”和“自动核验不到”。
- 从规则抽取源头控制规则粒度，避免依靠粗暴字符串拼接压缩到 30 条。
- 输出可直接用于比赛演示和人工查看的中文 Word 报告。

## 2. Trace 分析范围

本次分析以下两条独立投标文件 Trace：

```text
documents/traces/new/
├── full-trace-019efcea-2436-7cb0-be8b-8c49bbcaeed2.json
└── full-trace-019efce8-c9e7-7e01-8455-c9f8869ecf76.json
```

对应文件：

| Trace | 投标文件 |
|---|---|
| `019efcea-2436-7cb0-be8b-8c49bbcaeed2` | `招标采购测试集.docx` |
| `019efce8-c9e7-7e01-8455-c9f8869ecf76` | `物资类公开招标采购.docx` |

最终业务结果：

```text
data/outputs/audit-6/audit-6_审核结果.json
```

## 3. Trace 总体数据

### 3.1 `招标采购测试集.docx`

| 指标 | 结果 |
|---|---:|
| 根 Trace 耗时 | 约 108 秒 |
| 主 Agent 模型调用 | 8 次 |
| 全部 ChatModel 调用 | 20 次 |
| 主 Agent 提示词 Token 合计 | 约 18.9 万 |
| 全 Trace Token | 约 23.3 万 |
| 投标原文检索 | 5 次 |
| 招标原文检索 | 1 次 |
| 资料核验请求 | 10 次 |
| 资料原文检索 | 8 次 |
| 全部 Query | 54 个 |
| 字符串完全重复 Query | 0 |
| 最终规则结论 | 28 条 |
| 最终 JSON | 合法 |

资料核验结果：

| 状态 | 数量 |
|---|---:|
| `partially_supported` | 2 |
| `unsupported` | 7 |
| `unverifiable` | 1 |

### 3.2 `物资类公开招标采购.docx`

| 指标 | 结果 |
|---|---:|
| 根 Trace 耗时 | 约 89 秒 |
| 主 Agent 模型调用 | 8 次 |
| 全部 ChatModel 调用 | 17 次 |
| 主 Agent 提示词 Token 合计 | 约 24.6 万 |
| 全 Trace Token | 约 27.8 万 |
| 投标原文检索 | 5 次 |
| 招标原文检索 | 1 次 |
| 资料核验请求 | 10 次 |
| 资料原文检索 | 6 次 |
| 全部 Query | 51 个 |
| 字符串完全重复 Query | 0 |
| 最终规则结论 | 28 条 |
| 最终 JSON | 合法 |

资料核验结果：

| 状态 | 数量 |
|---|---:|
| `partially_supported` | 1 |
| `unsupported` | 7 |
| `unverifiable` | 2 |

## 4. 本轮已经确认有效的改进

### 4.1 文件级上下文已经隔离

两个投标文件分别形成独立根 Trace，不再放在同一 Thread 的两个 Turn 中。

每条 Trace 的 HumanMessage 只包含：

- 当前项目名称。
- 当前投标文件名称。
- 当前投标文件摘要。
- 当前规则。
- 当前投标方资料目录。

没有发现把另一个投标文件摘要注入当前主 Agent 的情况。

### 4.2 最后一轮强制输出机制有效

两条 Trace 都在第 8 轮：

```text
不再 bind_tools
→ 注入达到轮次上限的 HumanMessage
→ 模型输出完整 AgentAuditDecision JSON
```

最终均覆盖 28 条规则，没有再出现全部规则机械填充“达到步数上限”的情况。

该机制应继续保留，作为异常保护。

### 4.3 工具 Schema 和参数说明改进有效

本次 Trace 中：

- 没有工具参数校验错误。
- 每次检索 Query 数量没有超过 5。
- 没有猜测未授权 `file_id`。
- 没有调用不存在的工具。
- 资料子 Agent 能正确返回结构化结果。

工具参数说明已经有效，但 Trace 同时证明当前扁平 Query 和全局 Top K 返回结构不能稳定覆盖多个
审核问题。本任务需要调整工具输入输出结构，但不修改向量召回、关键词召回和 Rerank 算法。

### 4.4 工具并发执行逻辑正常

同一轮主 Agent 返回多个 `tool_calls` 时，LangGraph `ToolNode` 会并发执行这些工具，并等待本轮
全部工具返回后再进入下一轮主 Agent。

当前行为符合工具调用协议，不需要改成“某个工具一返回就立即进入下一轮 Agent”。

需要控制的是主 Agent 一轮发出多少无效工具，而不是改变 ToolNode 的等待语义。

## 5. 主要问题与根因

### 5.1 “原则上最多 3 轮”没有形成实际约束

当前提示词写的是：

```text
每个投标文件原则上最多进行3轮工具交互。
```

实际两条 Trace 的主 Agent 行为完全一致：

```text
第1轮：投标原文检索 + 招标原文检索
第2轮：投标原文检索
第3轮：投标原文检索
第4轮：投标原文检索
第5轮：投标原文检索
第6轮：5个资料核验任务
第7轮：5个资料核验任务
第8轮：强制输出
```

问题不在代码步数上限，而在提示词中的“原则上”属于弱约束，并且模型在普通轮次看不到：

- 当前是第几轮。
- 已经调用过各工具多少次。
- 剩余工具预算是多少。

模型只能看到不断累积的历史消息，无法稳定执行“3轮预算”。

除此之外，模型连续调用工具不完全是因为不遵守轮次约束。当前一次工具调用最多只能传入 5 个
扁平 Query，模型会把“一个审核问题的多个可能原文位置”理解为一组同类 Query。面对报价、
有效期、授权书、承诺函等多个审核问题时，5 个 Query 无法一次覆盖，模型自然会继续发起下一轮。

因此这里同时包含：

- 提示词没有形成硬预算。
- 工具一次可表达的审核问题数量不足。
- 工具没有显式区分“审核问题”和“该问题的多个 Query”。

### 5.2 Query 没有字符串重复，但存在严重语义重复

以 `招标采购测试集.docx` 为例，连续四轮查询：

```text
投标函 报价 金额 大写 小写
投标函 报价 金额 大写
投标函 一、投标函及投标函附录 报价
投标函 总价 大写 小写 金额
```

以及：

```text
投标有效期 90天 承诺
投标有效期 承诺 90天 投标函
投标有效期 承诺 投标函附录
投标有效期 90 天
```

这些 Query 字符串不同，但审核目标完全相同。

一个审核问题使用多个 Query 本身是合理的。例如核验报价时，投标函、报价表和货物清单可能分别
包含相关信息，不能强制只生成一个 Query。

真正的问题是当前工具只接收：

```json
{
  "queries": ["query-1", "query-2", "query-3"]
}
```

工具逐个执行检索后，将全部结果混合排序，并全局只保留 5 个片段。该实现位于：

```text
app/tools/bid_tools.py
├── SearchBidResponseInput.queries
├── SearchTenderSourceInput.queries
├── _search_many()
└── _compact_results()
```

如果简单把 Query 上限从 5 增加到 10，前几个审核问题的高分结果仍可能挤掉其他问题，导致某个
审核问题一个结果都没有返回。

因此需要同时解决两个问题：

> 判断是否重复应以核验事实为准，而不是以字符串是否相同为准。

并且：

> 工具输入需要按审核问题分组，每个审核问题允许多个互补 Query；工具输出也必须按审核问题分别
> 保留结果，不能将全部 Query 结果混合后只取一个全局 Top K。

### 5.3 摘要已经明确缺失，主 Agent 仍重复搜索

两个投标文件摘要中已经明确写出：

- 投标报价为空或未提供。
- 投标有效期未承诺。
- 法定代表人和授权代表人信息为空。
- 部分承诺函未确认。

主 Agent 仍然用 4～5 轮搜索这些事实。

当前提示词虽然写了“摘要已明确缺失的普通信息不要重复检索”，但没有区分：

- 摘要明确看到字段为空。
- 摘要只是没有提到该字段。
- 规则属于关键否决项，需要一次原文确认。

模型因此倾向于对所有关键项持续回查。

### 5.4 资料目录预检查没有执行

每条 Trace 都发出了 10 个资料核验任务，其中 7 个直接返回：

```json
{
  "status": "unsupported",
  "summary": "当前资料目录中没有可自动读取的对应资料，需结合人工核验情况判断"
}
```

这些调用在主 Agent 读取资料目录时就可以避免，例如：

- `other` 只有 `manual_review` 文件。
- `business_license` 只有 `manual_review` 文件。
- `test_report` 类别不存在。

当前提示词已经写明：

```text
只有manual_review文件时，不调用子Agent。
资料目录不存在对应类型时，不调用子Agent。
```

但该约束埋在较长提示词中，且没有要求模型在调用前做显式的三项检查：

1. 类别是否存在。
2. 类别下是否有 `classified` 文件。
3. 当前核验要求是否真的属于该类别。

### 5.5 一次资料核验任务混入多个资料类型

例如：

```json
{
  "rule_id": "rule-69",
  "evidence_type": "other",
  "verification_request": "核验人员关系说明以及企业营业执照副本扫描件"
}
```

人员关系说明和营业执照不是同一资料类型。

又如：

```json
{
  "rule_id": "rule-70",
  "evidence_type": "business_license",
  "verification_request": "核验投标人基本情况表以及近3年审计报告或财务报表"
}
```

基本情况、工商信息和财务报表也不应全部交给 `business_license` 类别。

当前规则经过合并后，一条规则可能包含多个独立审核事项和多个 `evidence_types`。一条规则需要
多类资料核验是正常的，问题不在于出现多个资料类别，而在于当前工具只能接收：

```json
{
  "rule_id": "rule-32",
  "verification_request": "……",
  "evidence_type": "certificate"
}
```

直接把 `evidence_type` 改成列表仍然不够，因为一个宽泛的 `verification_request` 会同时进入
多个类别，子 Agent 无法区分每类资料负责回答哪个事实。

更合理的粒度是：

```json
{
  "checks": [
    {
      "rule_id": "rule-32",
      "evidence_type": "business_license",
      "verification_request": "核验企业名称、经营状态和经营范围"
    },
    {
      "rule_id": "rule-32",
      "evidence_type": "certificate",
      "verification_request": "核验证书有效期及认证范围是否覆盖投标产品"
    }
  ]
}
```

即一次主 Agent 工具调用可以批量委派多类资料，但每个 `check` 仍保持：

- 一个 `rule_id`。
- 一个明确核验事实。
- 一个 `evidence_type`。
- 一组属于该事实的资料文件。

工具内部为每个 `check` 独立执行资料目录校验和子 Agent 核验，最后批量返回结果。

### 5.6 已经能够直接判断的事项仍被委派资料核验

`物资类公开招标采购.docx` 中，主 Agent 已经通过招标原文确认：

```text
本批次免收投标保证金。
```

第 7 轮仍然调用：

```json
{
  "rule_id": "rule-82",
  "evidence_type": "other",
  "verification_request": "核验投标保证金相关文件"
}
```

这说明当前提示词没有把“规则已判定为不适用或已获得充分结论”列为禁止委派条件。

### 5.7 “未召回”与“未提交”边界不够严格

资料子 Agent 只能检索主 Agent 指定的单个 `evidence_type`。

因此：

```text
在 contract 类别没有召回发票
```

只能说明：

```text
当前 contract 类别的可检索资料中没有找到发票。
```

不能自动扩大为：

```text
投标方没有提交发票。
```

只有以下情况才能判定为明确缺失或不合规：

- 投标文件摘要明确记录字段为空或文件缺失。
- 原文直接显示空白、未填写或未提交。
- 规则要求的完整范围已经被可靠检索，且没有相关内容。
- 资料目录明确证明应有类别不存在，并且该缺失本身构成规则不符合。

否则应使用 `manual_review` 或子 Agent 的 `unverifiable`，不能仅凭单个类别无召回直接认定
`non_compliant`。

### 5.8 子 Agent 第二轮仍有语义重复

部分资料子 Agent 第一轮未找到合同、发票或生产设备凭证后，第二轮继续使用：

```text
合同 发票 采购 凭证
生产场地 厂房 资质 证明
设备购置 合同 发票复印件
```

第二轮没有基于第一轮结果发现的新文件名、编号、日期或主体形成精确查询，只是改写了同义词。

当前“最多两轮检索”控制了最坏耗时，但没有规定第二轮的准入条件。

该问题也不只存在于提示词。`SearchEvidenceTool` 当前同样把所有 Query 的结果混合后全局只保留
5 个片段。第一轮如果同时核验多个子事实，某些子事实可能没有获得任何返回片段，子 Agent 只能
在下一轮继续查询。

因此需要同时修改：

- 提示词：第二轮只能基于第一轮产生的新精确线索。
- 工具：按核验问题组织多个 Query，并为每个问题独立保留结果。

### 5.9 合并规则内部包含过多独立审核事项

当前共 28 条规则，但总 `requirement` 约 7100 个中文字符。

部分单条规则长度：

| 规则 | 长度 | 情况 |
|---|---:|---|
| `rule-32` | 约 780 字 | 主体一致、授权、许可证、失信、行贿、破产等大量资格条件 |
| `rule-1` | 约 753 字 | 参数填写、格式、签章、授权书、身份证、承诺函等 |
| `rule-98` | 约 510 字 | 大量技术参数、环境条件、组件和供货范围 |
| `rule-18` | 约 509 字 | 容量、多个承诺函、专项文件、身份确认等 |

这会导致一条规则内部同时存在：

- 一部分符合。
- 一部分不符合。
- 一部分无法判断。

但当前输出 Schema 每条规则只能给一个 `result` 和一个 `reason`。

`rule-98` 并不表示最终曾经同时存在 98 条规则，但它证明规则 ID 已经至少递增到该数量级，说明
抽取阶段产生了大量细粒度候选规则。

当前规则抽取提示词虽然要求规则正文建议 40～160 个中文字符，但代码在规则超过 30 条后会按：

```text
category + priority + verification
category + priority
category + verification
category
```

分层执行确定性归并。归并正文采用 `requirement + requirement` 的分号拼接，不判断两个规则是否
属于同一个独立审核主题，因此最终形成 500～800 字的超长规则。

相关代码：

```text
app/rules/merger.py
├── _merge_requirements()
├── _merge_all_groups()
└── compact_rule_set()
```

规则抽取当前每批最多输入 12,000 字符，重叠 600 字符：

```text
app/rules/extractor.py
└── build_rule_extraction_batches(max_chars=12_000, overlap_chars=600)
```

较小批次会增加模型调用次数，也会让同一审核主题在不同批次中被重复抽取。当前模型上下文足够，
建议先扩大为每批 30,000 字符、重叠 1,000 字符，并移到配置文件。

本任务先从源头减少细碎规则：

- 扩大单批输入。
- 强化“独立审核结论”准入。
- 明确同一表格相关参数可以合并，不同资格、承诺、失信、授权事项不得仅因同类别而合并。
- 要求模型优先与当前规则库进行语义合并。

当前粗合并代码保留为兜底，但不能再把它当作主要的规则质量保障。完成新一轮规则抽取测试后，
如果仍频繁触发粗合并，再单独调整 `compact_rule_set()`；不能在没有新数据的情况下同时大改
提示词和压缩算法。

### 5.10 主模型上下文随工具轮次快速增长

两条 Trace 的主 Agent Prompt Token：

```text
招标采购测试集：
11270 → 21125 → 22307 → 23611 → 24792 → 26128 → 27014 → 27134

物资类公开招标采购：
11292 → 24364 → 27578 → 30420 → 33640 → 36869 → 37800 → 37862
```

首轮输入约 1.1 万 Token，可以接受。

主要增长来自多轮投标原文检索结果持续保留。即使模型支持大上下文，这种增长仍会：

- 增加耗时和成本。
- 让模型重复关注早期无效召回。
- 降低最终结论对关键证据的聚焦度。

本任务先通过减少工具轮次解决，不额外设计复杂的消息压缩或记忆系统。

### 5.11 最终来源引用需要更严格

当前最终 Finding 中存在以下情况：

- 某些结论使用了与结论关联较弱的宽泛片段。
- 某些通过资料子 Agent 得出的结论没有保留有效 `evidence_chunk_ids`。
- “未找到”类结论引用了一个只能证明文件存在、不能证明目标内容缺失的片段。

Chunk ID 只能表示：

> 该片段直接支持当前结论中的某个事实。

不能为了让报告“看起来可追溯”而附加弱相关片段。

## 6. 主 Agent 提示词改进方案

### 6.1 把软预算改为可执行预算

删除：

```text
每个投标文件原则上最多进行3轮工具交互。
```

改为：

```text
工具预算是硬约束：
1. search_bid_response在一个投标文件中最多调用1轮；一轮可提交多个审核问题，每个问题可提供
   1至3个互补query；
2. search_tender_source在一个投标文件中最多调用1轮；一轮可提交多个招标要求问题；
3. request_evidence_review最多集中调用1轮，一轮最多包含5个真正可自动核验的check；
4. 完成上述必要调用后，下一轮必须输出最终JSON；
5. 不要求把所有工具都调用一遍，没有信息缺口时可以直接输出。
```

这里限制的是工具轮次，不要求机械执行固定流程。

简单文件仍可以：

```text
第1轮直接输出
```

需要原文和资料核验的文件通常为：

```text
第1轮：一次性检索必要的投标原文和招标原文
第2轮：一次性委派必要的资料核验
第3轮：输出最终JSON
```

代码中的 8 轮上限继续保留，只作为异常保护。

### 6.2 每轮注入当前工具预算

普通轮次也需要让模型知道当前执行状态，不能只在最后一轮告诉模型轮次。

建议在每次主 Agent 调用前追加一条短 HumanMessage：

```text
当前主Agent第2轮。
已调用：search_bid_response=1，search_tender_source=1，request_evidence_review=0。
剩余预算：投标原文检索0轮，招标原文检索0轮，资料核验1轮。
本轮只能委派必要资料核验或直接输出最终JSON。
```

该消息由代码根据历史 `ToolMessage.name` 生成，不由模型自行统计。

伪代码：

```python
def build_turn_budget_message(state) -> HumanMessage:
    usage = count_tool_usage(state["messages"])
    return HumanMessage(
        content=render_prompt(
            "bid_agent_turn_budget",
            current_step=state["react_step_count"] + 1,
            bid_search_used=usage["search_bid_response"],
            tender_search_used=usage["search_tender_source"],
            evidence_review_used=usage["request_evidence_review"],
        )
    )
```

改动原因：

- 让预算成为模型每轮都能看到的当前约束。
- 避免模型在长工具结果之后忽略 SystemMessage 前部的轮次说明。

预期效果：

- 正常文件在 2～4 次主模型调用内完成。
- 第 8 轮强制输出只作为极少数异常兜底。

### 6.3 以“核验事实”判断重复

增加：

```text
判断检索是否重复时，以待核验事实为准，不以query字符串是否相同为准。
“投标函报价金额”“投标函总价大写小写”“报价金额是否填写”属于同一核验事实。
同一事实已经检索过一次后，禁止通过增加章节名、签字、提交、扫描件等近义词再次检索。
```

只有一种例外：

```text
前次返回片段明确给出新的文件名、章节名、编号或精确定位信息，
且该新定位可能改变结论时，才允许第二次定向检索。
```

同一个审核问题允许在同一轮提供多个互补 Query，但不允许跨轮改写同义词重复搜索。

### 6.4 明确摘要中三种“缺失”的处理

增加：

```text
摘要明确写明“为空、未填写、未承诺、未提供”的事实：
- 普通规则可直接判断；
- 关键否决规则最多进行一次原文确认；
- 原文确认后无新事实，立即停止，不得再次查询。

摘要只是没有提到某事项：
- 不能直接视为缺失；
- 仅在该事项会改变结论时进行一次原文检索。

摘要明确存在但细节不足：
- 只查询缺少的具体字段，不重新查询整个文件。
```

### 6.5 资料核验调用前执行严格三步检查

在提示词中把资料目录检查提升为独立强约束：

```text
调用request_evidence_review前必须同时满足：
1. evidence_type在资料目录中存在；
2. 该类别至少有一个status=classified的文件；
3. verification_request中的全部待核验事实都属于该evidence_type。

任一条件不满足，禁止调用子Agent。
只有manual_review资料时，直接输出人工复核。
类别不存在时，直接说明没有可自动核验的对应资料。
```

同时增加：

```text
已经通过摘要、投标原文或招标原文完成判断的规则，不得再委派资料核验。
已经确认不适用的规则，不得委派资料核验。
```

### 6.6 一次批量委派包含多个细粒度 Check

增加正反例：

```text
错误：
evidence_type=other
verification_request=核验人员关系说明和营业执照

正确：
checks=[
  {
    rule_id=rule-69,
    evidence_type=other,
    verification_request=核验是否提交人员关系说明及其内容是否完整
  },
  {
    rule_id=rule-69,
    evidence_type=business_license,
    verification_request=核验营业执照主体名称、经营状态和经营范围
  }
]
```

```text
错误：
evidence_type=business_license
verification_request=核验投标人基本情况表和近3年财务报表

正确：
只有资料目录存在对应classified类别时，才为该类别生成独立check；
不存在对应类别时直接人工复核，不得把多个资料强行塞入business_license。
```

同一条规则可以产生多个 `check`，但每个 `check` 只能核验一个资料类别内的一个明确事实。

### 6.7 限制资料核验任务数量

每个投标文件最多通过一次 `request_evidence_review` 提交 5 个资料核验 Check，优先级如下：

1. `critical + evidence_check`，且结果可能改变总体结论。
2. `important + evidence_check`，且资料目录存在可靠自动资料。
3. 其他确实不能通过摘要或投标原文判断的重点事项。

以下情况不占用子 Agent：

- `normal` 且不影响总体结论。
- 只有图片不可读资料。
- 类别不存在。
- 已经可以直接判断。
- 跨投标人比对、外部网站查询等当前系统无法完成的事项。

### 6.8 严格区分“没有召回”和“明确缺失”

增加：

```text
检索工具返回空chunks或子Agent返回unverifiable，只能说明当前自动检索没有得到可靠证据。
不得仅据此输出non_compliant。

只有摘要或原文明确显示未填写、为空、未提交、内容冲突或不满足数值要求时，
才能据此输出non_compliant。

自动检索没有找到，但规则要求必须人工查看原件、图片或外部系统时，输出manual_review。
```

`unsupported` 的处理：

```text
unsupported表示当前资料目录没有可自动核验资料，不等于投标方一定没有提交该材料。
主Agent应结合摘要决定：
- 摘要明确缺失：可判缺失；
- 摘要仅未提及：manual_review；
- 资料存在但不可读：manual_review并说明图片或正文不可读。
```

### 6.9 明确合并规则的综合判定

对于一个 `requirement` 中包含多个分号分隔审核事项的规则，主 Agent 应在内部逐项判断，再合成
一个规则结果。

综合结果优先级：

```text
任一明确否决项不满足       → non_compliant
没有明确否决，但存在明确风险 → risk
没有明确问题，但至少一项无法确认 → manual_review
全部适用事项均确认满足       → compliant
```

`reason` 应优先写：

1. 导致最终结果的决定性子项。
2. 已确认满足的关键子项。
3. 仍需人工复核的剩余子项。

不要只选择规则中的第一项进行判断。

说明：

该约束只能改善综合结论，不能从根本上解决规则被过度合并的问题。若后续发现一条规则经常同时
出现“部分不合规、部分人工复核”，仍应回到规则合并策略调整，而不是继续扩写主 Agent 提示词。

### 6.10 收紧来源引用

增加：

```text
只有片段直接证明当前reason中的事实时才填写chunk_id。
同一片段与结论只有主题相关、但没有直接事实支持时，不得引用。
“没有找到”本身不需要附加一个弱相关片段。
不得引用从未由工具返回的chunk_id。
```

## 7. 资料子 Agent 提示词改进方案

### 7.1 明确检索范围的语义

增加：

```text
你只能判断当前evidence_type和允许文件中的资料。
没有在当前范围召回某材料，不代表投标方在所有文件中都没有提交该材料。
```

子 Agent 输出应描述：

```text
在当前可自动读取的certificate资料中未找到……
```

而不是：

```text
投标方未提供……
```

### 7.2 第二轮检索增加准入条件

修改为：

```text
默认只进行1轮search_evidence。
只有第一轮结果中出现了新的精确文件名、主体名称、证书编号、合同编号、日期、金额、
产品型号或章节定位，并且该信息可能补足当前核验缺口时，才允许第2轮定向检索。
不得仅改写同义词或使用“合同、发票、凭证、证明”等更宽泛词再次检索。
```

### 7.3 输出状态边界

```text
supported：
当前资料原文直接、完整支持核验要求。

partially_supported：
部分关键事实得到原文支持，但仍缺少明确子项。

contradicted：
当前资料原文与核验要求或投标声明直接冲突。

ocr_required：
目标文件存在，但关键正文为图片或不可可靠读取。

unverifiable：
当前可自动检索范围不足以形成可靠结论。

unsupported：
由工具外层在当前类别没有classified资料时返回，子Agent正常执行时原则上不主动使用。
```

### 7.4 子 Agent Summary 写法

子 Agent Summary 应包含：

1. 已确认事实。
2. 直接证据。
3. 未确认缺口。
4. 当前结论边界。

示例：

```text
认证表显示质量管理体系证书有效期至2028年6月28日，但认证范围仅包含智能终端、
集成电路和低压电器，未明确包含红外热成像仪。因此证书有效期得到支持，
产品范围对应性仅部分支持。
```

避免：

```text
资料不完整，需要人工核验。
```

## 8. 分组检索与批量资料核验工具改造

### 8.1 投标原文和招标原文检索输入

`search_bid_response` 和 `search_tender_source` 不再接收扁平 `queries`，改为接收按审核问题组织的
`checks`：

```json
{
  "checks": [
    {
      "topic": "投标报价",
      "queries": [
        "投标函 报价 大写 小写",
        "货物清单 未含税单价 增值税税率"
      ]
    },
    {
      "topic": "投标有效期",
      "queries": [
        "投标函 投标有效期",
        "投标函附录 有效期 90天"
      ]
    }
  ]
}
```

建议约束：

- 每次最多 8 个审核问题。
- 每个问题提供 1～3 个互补 Query。
- `topic` 表达待核验事实，不写规则全文。
- 同一 `topic` 的 Query 可以搜索不同章节或文件名称。
- 不同 `topic` 不能合并为一个宽泛 Query。

### 8.2 分组召回和结果配额

当前实现将全部 Query 结果混合后全局取 5 个片段，需要改为：

```text
逐topic执行多个query
→ topic内合并和去重
→ topic内Rerank
→ 每个topic保留1～2个片段
→ 跨topic按chunk_id去重
→ 按总字符预算裁剪
```

返回结构：

```json
{
  "results": [
    {
      "topic": "投标报价",
      "chunks": [
        {
          "chunk_id": "chunk-1",
          "file_id": "file-55",
          "section": "投标函",
          "content": "……"
        }
      ]
    },
    {
      "topic": "投标有效期",
      "chunks": []
    }
  ]
}
```

空 `chunks` 只表示该 `topic` 没有召回可靠原文，不影响其他审核问题。

### 8.3 资料原文检索输入

`SearchEvidenceInput` 同样改为：

```json
{
  "checks": [
    {
      "topic": "证书有效期",
      "queries": [
        "质量管理体系认证 有效期",
        "ISO9001 有效期"
      ]
    },
    {
      "topic": "认证范围",
      "queries": [
        "认证范围 红外热成像仪",
        "认证事项范围 智能终端"
      ]
    }
  ],
  "file_ids": ["file-14"]
}
```

一个资料子 Agent 仍只访问一个 `evidence_type`，但可以在该类别内一次检索多个相关子事实。

### 8.4 批量资料核验工具

主 Agent 的 `request_evidence_review` 改为批量 Check：

```json
{
  "checks": [
    {
      "rule_id": "rule-32",
      "evidence_type": "business_license",
      "verification_request": "核验企业名称、经营状态和经营范围"
    },
    {
      "rule_id": "rule-32",
      "evidence_type": "certificate",
      "verification_request": "核验证书有效期及认证范围是否覆盖投标产品"
    }
  ]
}
```

工具内部：

1. 对每个 Check 独立检查资料类别是否存在 `classified` 文件。
2. 类别不存在时返回该 Check 的 `unsupported`；类别存在但只有图片不可读资料时返回
   `ocr_required`；两种情况都不启动子 Agent。
3. 存在资料时为该 Check 独立构造 `EvidenceReviewTask`。
4. 可以并发执行本批次内的子 Agent，但应设置小规模并发上限，避免同时启动过多模型请求。
5. 返回结果必须保留 `rule_id`、`evidence_type` 和对应核验结论。

返回示例：

```json
{
  "results": [
    {
      "rule_id": "rule-32",
      "evidence_type": "business_license",
      "status": "ocr_required",
      "summary": "当前营业执照资料为图片，无法自动读取",
      "evidence_chunk_ids": []
    },
    {
      "rule_id": "rule-32",
      "evidence_type": "certificate",
      "status": "partially_supported",
      "summary": "证书在有效期内，但认证范围未明确覆盖红外热成像仪",
      "evidence_chunk_ids": ["chunk-14-1-2-1"]
    }
  ]
}
```

## 9. 规则抽取输入与规则粒度优化

### 9.1 扩大规则抽取批次

当前代码：

```python
build_rule_extraction_batches(
    parsed,
    max_chars=12_000,
    overlap_chars=600,
)
```

建议改为配置：

```yaml
rule_extraction:
  batch_max_chars: 30000
  batch_overlap_chars: 1000
```

调用：

```python
contexts = build_rule_extraction_batches(
    parsed,
    max_chars=settings.rule_extraction.batch_max_chars,
    overlap_chars=settings.rule_extraction.batch_overlap_chars,
)
```

扩大批次的目的：

- 让模型在同一批次看到更完整的章节上下文。
- 减少同一审核主题跨批次重复抽取。
- 减少模型调用次数。
- 充分利用当前大上下文模型。

这不是把整个项目全部文件一次性送入模型。文件仍然串行处理，模型仍然看到当前完整规则库。

### 9.2 强化规则准入和独立结论要求

规则抽取提示词补充：

```text
一条规则必须能在最终报告中形成一个独立result和一个简短reason。
如果两个要求可能分别得到不同结论，就不得合并为同一条规则。

可以合并：
- 同一技术参数表中共同描述同一设备响应完整性的相关参数；
- 同一证明材料的名称、有效期和覆盖范围。

不得仅因category、priority或verification相同而合并：
- 营业执照与财务报表；
- 授权委托书与失信记录；
- 不同承诺函；
- 报价填写与投标有效期；
- 生产设备与历史业绩。
```

### 9.3 限制单批新增规则数量

提示词要求模型先检查当前规则库，再决定新增或合并：

```text
当前批次优先补充或合并已有独立审核主题。
只有当前规则库完全没有覆盖该独立审核主题时才add。
单批只输出对最终审核有明确影响的高价值规则，不为目录、格式标题、重复表格行建立规则。
```

不设置机械的每类下限。总量目标仍为整个项目不超过 30 条。

### 9.4 粗合并算法的处理边界

本轮先保留 `compact_rule_set()` 作为异常兜底，但需要增加可观测信息：

- 压缩前规则数量。
- 每层合并后的规则数量。
- 被合并的 Rule ID。
- 合并后 Requirement 长度。

第一步先用扩大批次和新提示词重新测试规则抽取。

如果仍频繁出现：

- 原始规则远超 30 条。
- 合并后单条 Requirement 超过约 300 字。
- 一个规则包含多个可分别形成结论的审核事项。

则停止进入 Agent 测试，单独调整规则抽取或 `compact_rule_set()`，不能继续用超长规则验证主 Agent。

## 10. 主 Agent 建议后的提示词结构

```text
# 角色与审核目标

# 输入可信层级
- 规则
- 当前投标文件摘要
- 当前资料目录
- 工具返回原文

# 合并规则综合判定方法

# 摘要明确缺失、未提及、细节不足的区别

# 工具调用硬预算

# 投标原文检索规则

# 招标原文检索规则

# 资料核验三步准入检查

# 未召回与明确缺失的区别

# Chunk引用规则

# 最终JSON要求

# 输出Schema
```

动态轮次消息单独放置：

```text
configs/prompts/templates/bid_agent_turn_budget.md
```

不要把轮次状态不断追加到固定 SystemMessage 中。

## 11. Word 报告问题分析

当前 Word 生成代码：

```text
app/reports/word_report.py
app/reports/styles.py
```

### 11.1 Markdown 被当作普通文本

`document_summary.summary` 本身是 Markdown：

```markdown
## 文件基本信息
- **投标方**：北京xx有限公司
```

当前代码直接执行：

```python
document.add_paragraph(file_result.document_summary.summary)
```

因此 Word 中直接出现：

```text
## 文件基本信息
- **投标方**：
```

### 11.2 英文枚举没有做展示映射

以下值直接通过 `str()` 写入 Word：

- `format`
- `qualification`
- `critical`
- `non_compliant`
- `manual_review`
- `matched`

JSON 可以继续保留英文稳定枚举，但 Word 必须统一翻译为中文。

### 11.3 五列表格不适合超长规则

当前审核明细使用：

```text
审核规则 | 类别 | 重要程度 | 结论 | 审核说明
```

规则正文可达到 700～800 字，放入窄表格后会出现：

- 每行只有几个字。
- 单行跨越多页。
- 其他列产生大片空白。
- 表格分页断裂。
- 阅读顺序混乱。

该问题不能只通过调整列宽解决。

### 11.4 报告缺少中文业务化结构

当前报告更像调试数据导出，缺少：

- 清晰的封面和任务信息。
- 中文总体结论。
- 各类结论数量概览。
- 风险和不合规项优先展示。
- 适合人工阅读的规则详情。
- 页眉、页脚和页码。
- 正式报告的段落、间距和分页控制。

## 12. Word 报告重构方案

### 12.1 基本原则

- JSON 仍是唯一事实来源。
- Word 不修改、过滤或重新判断 JSON 结论。
- Word 只负责中文化、结构化和视觉呈现。
- 不在 Word 生成阶段调用 LLM。
- 不直接展示 Markdown 标记和内部英文枚举。

### 12.2 中文枚举映射

在 `app/reports/styles.py` 或独立展示映射模块中维护：

```python
CATEGORY_LABELS = {
    "format": "格式与文件完整性",
    "qualification": "资格条件",
    "commercial": "商务响应",
    "technical": "技术响应",
    "performance_capacity": "履约与业绩能力",
    "consistency_risk": "一致性与风险",
}

PRIORITY_LABELS = {
    "critical": "关键",
    "important": "重要",
    "normal": "一般",
}

FINDING_LABELS = {
    "compliant": "符合",
    "non_compliant": "不符合",
    "risk": "存在风险",
    "manual_review": "需人工复核",
}

MATCH_LABELS = {
    "matched": "匹配",
    "not_matched": "不匹配",
    "uncertain": "无法确定",
}

CONCLUSION_LABELS = {
    "compliant": "总体符合",
    "non_compliant": "存在不符合项",
    "risk": "存在风险",
    "manual_review": "需人工复核",
    "not_applicable": "不适用",
}
```

### 12.3 解析摘要中的基础 Markdown

不引入完整 Markdown 渲染框架，先支持当前摘要实际使用的最小语法：

- `## 标题`
- `### 标题`
- `- 列表`
- `- **字段**：值`
- 普通段落

伪代码：

```python
def add_markdown_summary(document, text):
    for block in parse_simple_markdown(text):
        if block.type == "heading":
            document.add_heading(block.text, level=block.level)
        elif block.type == "field":
            add_label_value_paragraph(document, block.label, block.value)
        elif block.type == "bullet":
            document.add_paragraph(block.text, style="List Bullet")
        else:
            document.add_paragraph(block.text)
```

禁止把 `##`、`**` 等标记原样写入 Word。

### 12.4 报告整体结构

建议每个投标文件报告采用：

```text
封面
├── 电力智能招标审核报告
├── 项目名称
├── 投标文件
├── 投标方
├── 审核任务编号
└── 生成时间

一、审核结论概览
├── 总体结论
├── 符合/不符合/风险/人工复核数量
└── 总体审核总结

二、项目匹配情况
├── 匹配状态
└── 匹配理由

三、投标文件摘要
└── 将Markdown转换为Word标题、字段和列表

四、分类审核总结
├── 格式与文件完整性
├── 资格条件
├── 商务响应
├── 技术响应
├── 履约与业绩能力
└── 一致性与风险

五、重点问题清单
├── 不符合项
├── 风险项
└── 需人工复核项

六、逐规则审核明细
└── 按分类、重要性和rule_id排序

七、整改与复核建议

八、依据与证据索引
```

### 12.5 审核明细不再使用五列宽表

按分类建立二级标题，每条规则使用全宽块状结构：

```text
5.1 格式与文件完整性

规则 rule-1  [关键]  [不符合]
审核要求：
……

审核结论：
……

引用依据：
投标原文：文件名 / 章节
```

伪代码：

```python
for category, findings in grouped_findings:
    document.add_heading(category_label(category), level=2)
    for finding in findings:
        add_rule_header(
            rule_id=finding.rule_id,
            priority=priority_label(rule.priority),
            result=finding_label(finding.result),
        )
        add_labeled_paragraph("审核要求", rule.requirement)
        add_labeled_paragraph("审核结论", finding.summary)
        add_source_links(finding)
```

这样即使规则正文较长，也能使用完整页面宽度，不会在窄单元格中纵向堆字。

### 12.6 重点问题优先展示

在完整明细前，增加精简问题清单：

| 序号 | 分类 | 结论 | 问题摘要 |
|---:|---|---|---|

该表只展示 Finding Summary，不重复完整规则正文，因此可以保持正常列宽。

顺序：

```text
不符合 → 风险 → 人工复核 → 符合
```

### 12.7 证据索引展示真实信息

Word 中不只展示内部 Chunk ID。

优先展示：

- 来源类型。
- 文件名。
- 章节。
- 原文摘录。

Chunk ID 可以作为技术追踪信息放在最后一列或附录中，但不能成为用户看到的主要内容。

```text
招标原文｜招标文件-脱敏.docx｜投标人须知前附表｜“……”
投标原文｜招标采购测试集.docx｜投标函｜“……”
支撑资料｜业绩文件.docx｜发票目录表｜“……”
```

### 12.8 页面和样式

`apply_document_styles()` 至少补充：

- A4 页面。
- 合理页边距。
- 中文正文、标题字体。
- 标题字号和段前段后距离。
- 正文 1.5 倍行距或固定合理行距。
- 页眉显示项目简称。
- 页脚显示页码和“电力智能招标审核系统”。
- 标题与后续正文保持在同页。
- 表头跨页重复。
- 简短表格行禁止被跨页拆分。
- 结论颜色只作为辅助，不依赖颜色表达唯一含义。

建议颜色：

| 状态 | 颜色 |
|---|---|
| 符合 | 绿色 |
| 不符合 | 红色 |
| 风险 | 橙色 |
| 人工复核 | 灰蓝色 |

### 12.9 不适用文件报告

`not_matched` 文件仍然生成 Word，但只包含：

- 文件基本信息。
- 项目匹配状态。
- 不匹配理由。
- 未进入规则审核的说明。

不输出空的审核明细、风险表和证据索引。

## 13. 计划修改文件

```text
configs/prompts/templates/
├── rule_extraction.md              # 独立审核主题和语义合并边界
├── bid_agent.md                    # 主Agent硬预算、资料准入、缺失边界
├── bid_agent_turn_budget.md        # 每轮动态工具预算提示
└── evidence_subagent.md            # 子Agent第二轮准入和结论边界

configs/
└── model.yaml 或独立规则配置       # 规则抽取批次字符和重叠字符

app/agents/
├── bid_agent.py                    # 统计工具使用并注入当前预算
└── evidence_subagent.py            # 保留最终轮禁用工具机制

app/tools/
├── bid_tools.py                    # 分组检索、分组结果和批量资料Check
└── evidence_tools.py               # 资料分组检索和每个问题独立结果

app/models/
└── evidence.py                     # 必要时增加批量资料核验输入输出模型

app/rules/
├── extractor.py                    # 使用可配置的30000字符批次
└── merger.py                       # 本轮只补充压缩过程观测，不立即重写算法

app/reports/
├── word_report.py                  # 重构报告结构和Markdown渲染
└── styles.py                       # 中文映射、字体、页面、状态样式

tests/
├── unit/rules/
│   └── test_rule_extraction_batches.py
├── unit/tools/
│   └── test_grouped_search_tools.py
├── unit/agents/
│   └── test_turn_budget.py         # 工具预算消息
├── unit/reports/
│   └── test_word_report.py         # 中文化和报告结构
└── integration/
    └── test_audit_graph.py         # 主Agent收敛和资料调用准入
```

本任务原则上不修改：

- 向量相似度、关键词召回和 Rerank 评分算法。
- 向量索引。
- ToolNode 并发等待逻辑。
- RuleSet 持久化模型。
- 最终 JSON Schema。
- 路由和投标文件摘要流程。

本任务会修改检索工具的参数结构、结果分组和结果配额，但不更换底层检索算法。

## 14. 核心伪代码

### 14.1 主 Agent 每轮预算

```python
def count_main_tool_usage(messages):
    return {
        "search_bid_response": count_tool_messages(
            messages, "search_bid_response"
        ),
        "search_tender_source": count_tool_messages(
            messages, "search_tender_source"
        ),
        "request_evidence_review": count_tool_rounds(
            messages, "request_evidence_review"
        ),
    }


async def bid_agent_node(state, model, tools, max_steps):
    current_step = state["react_step_count"] + 1
    messages = build_bid_agent_messages(state)
    messages.append(build_turn_budget_message(state))

    if current_step >= max_steps:
        messages.append(build_force_finalize_message(current_step, max_steps))
        response = await model.ainvoke(messages)
    else:
        response = await model.bind_tools(tools).ainvoke(messages)

    return {
        "messages": [response],
        "react_step_count": current_step,
    }
```

预算消息只负责告诉模型边界，不在代码层粗暴删除模型返回的合法工具调用。若 Trace 证明模型仍持续
违反预算，再评估是否在路由层增加确定性保护。

### 14.2 分组检索

```python
async def search_grouped(checks, query_filter):
    grouped_results = []
    for check in checks:
        candidates = []
        for query in deduplicate(check.queries):
            candidates.extend(
                await retrieval.search(
                    query,
                    query_filter,
                    candidate_top_k=20,
                    final_top_k=8,
                )
            )
        grouped_results.append(
            {
                "topic": check.topic,
                "chunks": compact_per_topic(
                    candidates,
                    top_k=2,
                    per_chunk_chars=1800,
                ),
            }
        )
    return apply_total_character_budget(grouped_results)
```

### 14.3 批量资料核验

```python
async def request_evidence_reviews(checks, state):
    async def execute(check):
        available = classified_files(
            state.evidence_catalog,
            check.evidence_type,
        )
        if not available:
            return unsupported_result(check)
        return await run_evidence_subagent(check, available)

    # 限制小规模并发，避免一次启动过多模型调用。
    return {
        "results": await gather_with_limit(
            [execute(check) for check in checks],
            limit=3,
        )
    }
```

### 14.4 规则抽取批次

```python
contexts = build_rule_extraction_batches(
    parsed,
    max_chars=settings.rule_extraction.batch_max_chars,
    overlap_chars=settings.rule_extraction.batch_overlap_chars,
)
```

默认配置：

```yaml
rule_extraction:
  batch_max_chars: 30000
  batch_overlap_chars: 1000
```

### 14.5 Word 报告生成

```python
def render_file_report(result, file_result):
    document = Document()
    apply_document_styles(document)

    add_cover(document, result, file_result)
    add_conclusion_overview(document, file_result)
    add_project_match(document, file_result.project_match)

    if not file_result.is_applicable:
        return document

    add_markdown_summary(document, file_result.document_summary.summary)
    add_category_summaries(document, file_result.category_summaries)
    add_priority_issue_table(document, file_result.findings)
    add_finding_sections(document, result.rules, file_result.findings)
    add_suggestions(document, file_result.suggestions)
    add_source_appendix(document, collect_source_index(result, file_result))
    return document
```

## 15. 验收指标

### 15.1 Trace 验收

使用相同 `audit-6` 数据或重新上传同一测试集后：

- 每个匹配投标文件继续保持独立 Trace。
- 主 Agent 正常情况下不超过 4 次模型调用。
- `search_bid_response` 每个文件不超过 1 轮。
- `search_tender_source` 每个文件不超过 1 轮。
- 单次原文检索可以包含多个 `topic`，每个 `topic` 允许 1～3 个互补 Query。
- 每个 `topic` 独立返回结果，不因其他问题得分较高而完全丢失。
- `request_evidence_review` 每个文件不超过 1 轮、5 个 Check。
- 明知类别不存在或只有 `manual_review` 时，资料核验调用数为 0。
- 不再对报价、投标有效期、授权委托书等同一事实连续改写 Query。
- 最终 JSON 仍完整覆盖 28 条规则。
- 最终 JSON Schema 校验通过。
- 主 Agent Prompt Token 合计目标控制在每文件 10 万以内。

### 15.2 结论质量验收

- “当前类别未召回”不再直接被写成“投标方未提交”。
- 明确空白字段可以判定不合规。
- 图片不可读资料进入人工复核。
- 已确认不适用的保证金规则不再调用资料子 Agent。
- 每个资料核验任务只处理一个资料类别内的一个明确问题。
- 合并规则的结论能够覆盖决定性不合规项和剩余人工复核项。
- Finding 中只保留直接支持结论的 Chunk ID。

### 15.3 规则抽取验收

- 单批招标文件输入上限调整为 30000 字符、重叠 1000 字符。
- 相同测试招标文件的规则抽取模型调用次数明显下降。
- 最终规则总量不超过 30 条。
- 单条规则原则上不超过约 300 字。
- 不再仅因分类、重要程度或核验方式相同而形成跨主题长规则。
- 同一技术表中的相关参数可以作为一个审核主题。
- 营业执照、财务报表、承诺函、授权、失信等可分别形成结论的事项不能粗暴拼为一条。
- 如果仍触发粗合并并产生超长规则，停止后续 Agent 测试并先处理规则质量。

### 15.4 Word 验收

使用：

```text
data/outputs/audit-6/audit-6_审核结果.json
```

重新生成 Word，并完成逐页渲染检查：

- Word 中不出现 `##`、`**` 等 Markdown 标记。
- Word 中不直接出现 `format`、`critical`、`non_compliant` 等英文枚举。
- 超长规则不再放入五列窄表格。
- 不出现规则正文被挤成纵向单字列的情况。
- 不出现由表格跨页造成的大面积无意义空白。
- 不合规、风险、人工复核项在报告前部可以快速查看。
- 每条规则仍能查看完整审核要求和审核结论。
- 证据索引优先展示文件名、章节和摘录。
- 不匹配文件生成简洁的不适用报告。
- JSON 内容不因 Word 展示重构而被修改或遗漏。

## 16. 实施顺序

本任务按以下顺序执行：

1. 先修改规则抽取提示词和批次字符配置。
2. 只重新运行项目规则抽取，检查规则数量、长度和语义边界。
3. 规则质量合格后，修改分组检索和批量资料核验工具。
4. 修改主 Agent、资料子 Agent 提示词和动态工具预算消息。
5. 重新运行资料分类和单个投标文件审核，分析新 Trace。
6. 确认工具调用和最终结论收敛后，再修改 Word 生成代码。
7. 使用现有 `audit-6` JSON 进行 Word 快速迭代。
8. 渲染全部 Word 页面并检查版式。
9. 最后运行单元测试和集成测试。

每个阶段单独保留测试结果和 Trace，避免同时改变多个变量后无法判断改善来源。资料分类提示词本轮
原则上保持不变，除非新 Trace 明确证明分类结果本身阻塞资料核验。
