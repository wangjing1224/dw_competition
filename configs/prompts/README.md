# Prompt 配置说明

本目录保存模型可调整的文本配置。Python 代码只负责选择输入、注入运行时变量和校验输出。

## 模板占位符来源

### `templates/rule_extraction.md`

| 占位符 | 注入位置 | 内容 |
|---|---|---|
| `${project_id}` | `app/rules/extractor.py` | 当前新建项目的 ID |
| `${source_context}` | `app/rules/extractor.py` | 解析后的章节标题、正文和表格 |
| `${rule_extraction_schema}` | `app/rules/extractor.py` | `RuleExtractionBatch` 动态生成的 JSON Schema |

### `templates/evidence_classification.md`

| 占位符 | 注入位置 | 内容 |
|---|---|---|
| `${classification_input}` | `app/evidence/classifier.py` | 文件名、相对目录、章节、首尾正文和解析状态 |
| `${evidence_types}` | `app/evidence/classifier.py` | `EvidenceType` 枚举的全部可选值 |
| `${evidence_classification_schema}` | `app/evidence/classifier.py` | `EvidenceClassification` 动态生成的 JSON Schema |

### `templates/bid_agent.md`

| 占位符 | 注入位置 | 内容 |
|---|---|---|
| `${bid_agent_decision_schema}` | `app/agents/bid_agent.py` | `BidAgentDecision` 判别联合动态生成的 JSON Schema |

`app/agents/bid_agent.py` 还会在运行时追加 HumanMessage：

- `task_id/project_id/bidder_id`
- 当前投标文件
- 当前 RuleItem 或项目适用性工作单元
- 已有相关 Finding
- 当前工作单元内的 AIMessage/ToolMessage 历史

### `templates/evidence_subagent.md`

| 占位符 | 注入位置 | 内容 |
|---|---|---|
| `${evidence_review_result_schema}` | `app/agents/evidence_subagent.py` | `EvidenceReviewResult` 动态生成的 JSON Schema |

`app/agents/evidence_subagent.py` 还会在运行时追加 HumanMessage：

- 当前 Claim
- 当前 Claim 内的 AIMessage/ToolMessage 历史

## 修改约束

- 使用 `$name` 或 `${name}` 声明占位符。
- 新增占位符后，必须在对应 Python 调用 `render_prompt()` 时提供同名参数。
- 缺少参数会直接抛错，不会把 `${name}` 原样发送给模型。
- 输出字段结构以 Pydantic Schema 为准，不在 Prompt 中手工维护第二份字段定义。
- `tool_descriptions.yaml` 只修改模型看到的工具说明，不修改工具名和参数 Schema。
