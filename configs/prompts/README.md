# Prompt 配置说明

本目录保存模型可调整的文本配置。每类调用拆成：

- `xxx.md`：稳定的 SystemMessage，包含角色、边界、判断原则和轻量输出 Schema。
- `xxx_input.md`：本次运行的 HumanMessage，包含文件、规则、摘要、资料目录等动态数据。

工具名称、说明和参数 Schema 由 LangChain `bind_tools` 注入，不在 System Prompt 中重复维护。

## 模板与占位符

| 模板 | 主要占位符 | Python 注入位置 |
|---|---|---|
| `rule_extraction.md` | `${rule_delta_schema}` | `app/rules/extractor.py` |
| `rule_extraction_input.md` | `${current_rules}`、`${source_file}`、`${batch_index}`、`${batch_count}`、`${source_context}` | `app/rules/extractor.py` |
| `evidence_classification.md` | `${evidence_types}`、`${evidence_classification_schema}` | `app/evidence/classifier.py` |
| `evidence_classification_input.md` | `${classification_input}` | `app/evidence/classifier.py` |
| `bid_document_summary.md` | `${bid_document_summary_schema}` | `app/audit/bid_summary.py` |
| `bid_document_summary_input.md` | `${file_identity}`、`${project_profile}`、`${rule_categories}`、`${document_context}` | `app/audit/bid_summary.py` |
| `bid_agent.md` | `${agent_audit_decision_schema}` | `app/agents/bid_agent.py` |
| `bid_agent_input.md` | `${project_name}`、`${bid_file}`、`${bidder}`、`${bid_summary}`、`${rule_groups}`、`${evidence_catalog}` | `app/agents/bid_agent.py` |
| `evidence_subagent.md` | `${evidence_review_schema}` | `app/agents/evidence_subagent.py` |
| `evidence_subagent_input.md` | `${verification_request}`、`${evidence_type}`、`${evidence_files}` | `app/agents/evidence_subagent.py` |

## 修改约束

- 使用 `$name` 或 `${name}` 声明占位符。
- 新增占位符后，必须在对应 `render_prompt()` 调用中提供同名参数。
- 缺少参数会立即报错，不会把占位符原样发送给模型。
- 输出结构以轻量 Pydantic DTO 生成的 Schema 为准。
- `tool_descriptions.yaml` 只修改模型看到的工具说明，不修改工具名或参数 Schema。
- 业务身份、来源对象和完整领域模型不应为了方便而重新塞入 LLM 输入。
