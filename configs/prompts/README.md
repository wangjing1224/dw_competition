# Prompt 配置说明

本目录保存模型可调整的文本配置。每类调用拆成：

- `xxx.md`：SystemMessage，包含角色、边界和判断原则；一次性结构化调用附输出 Schema，规则 Agent 使用原生工具参数 Schema。
- `xxx_input.md`：本次运行的 HumanMessage，包含文件、规则、摘要、资料目录等动态数据。

工具名称、说明和参数 Schema 由 LangChain `bind_tools` 注入，不在 System Prompt 中重复维护。

## 模板与占位符

| 模板 | 主要占位符 | Python 注入位置 |
|---|---|---|
| `rule_extraction.md` | `${rule_view}`（启动快照，会话内不覆盖） | `app/rules/extractor.py` |
| `rule_extraction_input.md` | `${file_id}`、`${file_name}`、`${source_path}`、`${history_files}`、`${source_context}` | `app/rules/extractor.py` |
| `rule_extraction_last_tool_round.md`、`rule_extraction_finalize.md` | `${current_step}`、`${max_steps}` | `app/agents/rule_agent.py` |
| `evidence_ingestion.md` | `${current_material}`、`${history_view}`（启动快照） | `app/evidence/ingestion.py` |
| `evidence_ingestion_input.md` | `${file_id}`、`${file_name}`、`${source_path}`、`${reading_scope}`、`${continuation}`、`${source_context}` | `app/evidence/ingestion.py` |
| `evidence_ingestion_last_tool_round.md`、`evidence_ingestion_finalize.md` | `${current_step}`、`${max_steps}` | `app/agents/evidence_ingestion_agent.py` |
| `bid_document_summary.md` | `${bid_route_schema}`，仅匹配状态及理由 | `app/audit/bid_summary.py` |
| `bid_document_summary_input.md` | `${file_identity}`、`${project_profile}`、`${reading_scope}`、`${document_context}` | `app/audit/bid_summary.py` |
| `bid_agent.md` | `${rule_view}`、`${report_markdown_contract}` | `app/agents/bid_agent.py` |
| `bid_agent_input.md` | `${file_identity}`、`${project_profile}`、`${reading_scope}`、`${document_context}`、`${evidence_catalog}`、`${report_content}`、`${continuation}` | `app/agents/bid_agent.py` |
| `evidence_subagent.md` | 无动态变量，不绑定最终输出 Schema | `app/agents/evidence_subagent.py` |
| `evidence_subagent_input.md` | `${verification_request}`、`${file_ids}`、`${relations}`、`${evidence_files}`、`${continuation}` | `app/agents/evidence_subagent.py` |
| `bid_agent_last_tool_round.md`、`bid_agent_finalize.md` | `${current_step}`、`${max_steps}`、`${missing}` | `app/audit/session.py` |
| `evidence_subagent_last_tool_round.md`、`evidence_subagent_finalize.md` | `${current_step}`、`${max_steps}` | `app/audit/session.py` |

## 独立报告模板

[audit_report_template.md](templates/audit_report_template.md) 单独维护报告结构和填写要求，是无业务占位符的静态 System 片段，不是已生成的报告正文。通过 `${report_markdown_contract}` 注入主 Agent 的 System；Human 和工具说明不重复模板。任务创建时固定此模板，批次与压缩续跑沿用快照。

主 Agent 通过 `report_document` 保存 Markdown，最终回复只说明处理情况；子 Agent 直接返回核验正文。报告初始化、草稿/最终校验和 MD 转 JSON 位于 `app/reports/markdown_report.py`，更换模板结构须同步调整解析器。`AgentAuditDecision` 仅为代码解析后的内部对象，不再注入模型。

## 修改约束

- 使用 `$name` 或 `${name}` 声明占位符。
- 新增占位符后，必须在对应 `render_prompt()` 调用中提供同名参数。
- 缺少参数会立即报错，不会把占位符原样发送给模型。
- 路由输出以 `BidRouteDecision` 的两字段 Schema 为准；Agent 不绑定最终 JSON Schema，工具参数仍严格校验。
- `tool_descriptions.yaml` 只修改模型看到的工具说明，不修改工具名或参数 Schema。
- 业务身份、来源对象和完整领域模型不应为了方便而重新塞入 LLM 输入。
