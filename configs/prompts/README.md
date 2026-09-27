# Prompt 配置说明

本目录保存模型可调整的文本配置。每类调用拆成：

- `xxx.md`：SystemMessage，包含角色、边界和判断原则；一次性结构化调用附输出 Schema，规则 Agent 使用原生工具参数 Schema。
- `xxx_input.md`：本次运行的 HumanMessage，包含文件、规则、摘要、资料目录等动态数据。

工具名称、说明和参数 Schema 由 LangChain `bind_tools` 注入，不在 System Prompt 中重复维护。

## 模板与占位符

| 模板 | 主要占位符 | Python 注入位置 |
|---|---|---|
| `rule_extraction.md` | `${rule_view}`（启动快照，会话内不覆盖） | `app/rules/extractor.py` |
| `rule_extraction_input.md` | `${file_id}`、`${file_name}`、`${source_path}`、`${history_files}`、`${reading_scope}`、`${continuation}`、`${source_context}` | `app/rules/extractor.py` |
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
| `context_compaction.md` | 无；System交接摘要约束 | `app/llm/context_compaction.py` |
| `parser_image_transcription.md` | 无；System图表/图片转写要求 | `app/parsers/vision.py`，直接读取全文 |
| `parser_table_merge.md` | 无；System跨页续表合并要求 | `app/parsers/vision.py`，直接读取全文 |
| `audit_report_template.md` | 无；主Agent System中的报告模板片段 | 审核任务固定快照，经 `app/agents/bid_agent.py` 注入 |
| `evidence_classification.md` | `${evidence_types}`、`${evidence_classification_schema}` | `app/evidence/classifier.py`，旧单次分类入口，当前资料入库Agent不调用 |
| `evidence_classification_input.md` | `${classification_input}` | 同上，旧入口Human模板，保留供参考/契约测试 |

### 占位符类型、来源和空值约定

占位符没有模板内默认值：调用方必须提供。`render_prompt` 用 `string.Template.substitute` 渲染；
字符串原样保留，dict/list/Pydantic模型才序列化为JSON，其余类型转成字符串。
不要用`None`代替缺项，它会变成字面量`None`。下面未注明可空的变量都应由调用方提供有意义的值。

| 变量 | 类型、来源与用途 | 可空/缺省处理 |
| --- | --- | --- |
| `rule_view` | str，`app/rules/context.py` 的确定性Markdown渲染，包含规则ID、类别、要求、来源等；主审核不重复项目简介 | 空规则库仍有渲染出的结构提示，不省略变量 |
| `file_id` / `file_name` / `source_path` | str，当前解析文档的登记身份，系统生成ID，文件名/上传路径不是模型推断结果 | 有登记记录后才调用，不让模型补身份 |
| `history_files` | str，规则侧已完成历史文件的ID/名称列表，不包括未来文件 | 无则“暂无已处理历史文件” |
| `reading_scope` | str，代码按阅读游标构造批次/结构范围及是否还有后文；路由注明只看前部或全文 | 必填；不能把未读后文描述为已读 |
| `source_context` | str，规则/资料侧 `render_document_range` 渲染本批正文、表格、图片路径 | 按解析与预算选择完整结构；不补造缺失原文 |
| `continuation` | str，代码构造首批/续批提示；压缩时附交接摘要 | 无交接时仍给首批/独立任务说明，不当作原始证据 |
| `current_material` | str，资料库当前文件的已保存双摘要与关系视图，`render_material` 渲染 | 尚未写入则展示“尚无已保存结果” |
| `history_view` | str，已完成资料的历史摘要视图，`render_history` 渲染，详细程度按预算组织 | 无则“暂无已完成历史资料” |
| `bid_route_schema` | str，`BidRouteDecision.model_json_schema()`序列化结果，仅match_status/match_reason | 必填，修改输出字段须同步数据类及校验，不能只改模板 |
| `file_identity` | str，代码拼接当前文件ID/名称，主审核还带所选项目和资料库 | 只放当前文件，不注入整批其他标书身份 |
| `project_profile` | str，当前RuleSet的项目简介，用于路由/主审核了解项目 | 库中可能为空，不能虚构项目或默认判匹配 |
| `document_context` | str，路由/主审核本次预算允许的原文结构；不再是另一个模型的标书摘要 | 同source_context，由解析质量及预算决定 |
| `evidence_catalog` | str，`brief_catalog`渲染file_id与bid_summary，给主Agent选择核验文件 | 无则“无资料”，不补文件名或再次聚合摘要 |
| `report_markdown_contract` | str，独立报告模板在任务创建时的固定快照，主审核System输入 | 必填，跨批/压缩不悄悄替换为磁盘新版 |
| `report_content` | str，当前文件已持久化的Markdown草稿，主Agent Human中使用 | 初始由报告模块初始化，不代表审核已完成 |
| `verification_request` | str，主Agent委派的具体核验要求，子Agent Human输入 | 不得为空，工具参数层校验 |
| `file_ids` | str，工具起始文件ID列表连接成文本，不是原始list；子Agent核验范围入口 | 必须存在合法起始文件 |
| `relations` | str，`evidence_view`渲染起始文件及直接邻接关系 | 无则“无直接关联” |
| `evidence_files` | str，选中/关联资料按ID渲染的evidence_summary；定位线索不是核验依据 | 已选择有效资料才调用；图片路径按摘要与原文实际可读性使用 |
| `current_step` / `max_steps` | int，节点在调用前计算的当前调用序号/上限 | 必填；Human提醒不能代替代码是否绑定工具的判断 |
| `missing` | str，报告草稿检查发现的缺项，经 `app/audit/session.py` 拼接 | 无则代码传“无”，不是让模型杜撰遗漏 |
| `evidence_types` | list[str]，旧分类入口的 `EvidenceType` 枚举值 | 由代码完整提供，渲染为JSON列表 |
| `evidence_classification_schema` | dict，旧 `EvidenceClassification` 的JSON Schema | 由代码提供，旧入口与当前双摘要库不是同一协议 |
| `classification_input` | dict，旧分类入口的文件名/相对目录/前20个标题/正文前4000与后2000字符/解析状态/图片数/警告 | 尾部短时为空，标题/警告可空列表；这是旧入口的裁剪，不是当前入库Agent策略 |

### 特殊模板的消息组织

- `*_last_tool_round.md`：倒数第二轮追加的Human提醒，仍绑定工具；`*_finalize.md`：最后一轮Human提醒，不再绑定工具。
  规则/资料入库结束只回处理说明；主审核必须此前已通过工具保存报告；子审核结束直接回核验正文。
- `context_compaction.md`：无工具的System提示。Human由代码拼接业务权威状态和原会话时序转录，输出交接文本，
  不改写规则/资料库；续跑的业务适配器负责读取最新状态、重新组织System与当前阅读范围。
- 两份`parser_*.md`：无业务占位符，视觉模块直接读文件，Human另外附图片上下文/相邻原文与多模态图片。
  转写输出与 `ImageReading`、续表合并结果校验配套；不是业务Agent的`view_image`工具说明。
- `evidence_classification*.md`：保留的旧入口模板，不代表当前资料入库仍调用它；不要在这里修改后期待资料Agent行为改变。

## 工具说明配置

`tool_descriptions.yaml` 中六个字符串分别描述 `search_documents`、`view_image`、`submit_rule_delta`、
`save_evidence_material`、`request_evidence_review`、`report_document`。通用检索/看图由四类Agent复用，
其余分别由规则入库、资料入库、主审核使用。入参Field说明与Schema在 `app/tools/` 中维护，
返回结果由工具实现渲染；只改YAML不会改变权限、参数个数、返回字段或读写行为。

例如扩充检索使用说明，应改`search_documents`对应块字符串；扩充入参则需同步
`app/tools/common_tools.py`中的输入模型、校验、工具实现与测试，不能仅在文字里承诺支持新参数。

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
- `${...}`之外的美元符号若经过`render_prompt`须写为`$$`；直接读文件的静态parser模板不做此替换。
- 修改后重启服务刷新缓存；代码会缓存模板与工具说明，已创建任务还可能持有旧模板快照。
- 维护说明放在本README，工具配置用块字符串外的YAML注释；模板全文（包括HTML注释）可能进入模型。
  本轮只补文档，不改变任何模板正文、工具描述字符串或报告结构。
