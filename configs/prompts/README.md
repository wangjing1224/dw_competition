# Prompt 配置说明

本目录保存模型可调整的文本配置。每类调用拆成：

- `xxx.md`：SystemMessage，包含角色、边界、判断原则与工具使用策略；一次性结构化调用附输出 Schema，规则 Agent 使用原生工具参数 Schema。
- `xxx_input.md`：本次运行的 HumanMessage，包含文件、规则、摘要、资料目录等动态数据。

工具定义及参数 Schema 由 LangChain `bind_tools` 注入。System 用实际工具名作小标题，组织本角色的使用规范，不重复维护完整契约和参数 Schema。Python 字段描述负责参数含义与约束，YAML 工具描述负责职责、权限、副作用、返回和错误语义；何时检索、委派、保存等策略集中在相应 System，不由共享工具说明规定工作顺序。

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
| `tool_descriptions.yaml::search_documents` | `${candidate_top_k}`、`${final_top_k}` | `app/tools/common_tools.py` 创建工具时，用该会话检索配置单独渲染 |

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
| `evidence_files` | str，选中/关联资料按ID渲染的evidence_summary；事实充分时可直接依据摘要回答，标明依据类型 | 已选择有效资料才调用；摘要不等于已回读原件，缺项/冲突/视觉需求再补查 |
| `current_step` / `max_steps` | int，节点在调用前计算的当前调用序号/上限 | 必填；Human提醒不能代替代码是否绑定工具的判断 |
| `missing` | str，报告草稿检查发现的缺项或校验错误，经 `app/audit/session.py` 拼接 | 无则代码传“无”，不是让模型杜撰遗漏 |
| `candidate_top_k` | int，`settings.retrieval.candidate_top_k`；每 query、每 scope 内向量和关键词各自的初始候选上限，两路融合后可能更多 | 由配置提供，不是工具参数 |
| `final_top_k` | int，`settings.retrieval.final_top_k`；每 query、每 scope 的 Rerank top_n／最终命中上限，不是全组或全调用总上限 | 由配置提供，不是工具参数 |

### 特殊模板的消息组织

- `*_last_tool_round.md`：倒数第二轮追加的Human提醒，仍绑定工具；`*_finalize.md`：最后一轮Human提醒，不再绑定工具。
  规则/资料入库结束只回处理说明；主审核必须此前已通过工具保存报告；子审核结束直接回核验正文。
- `context_compaction.md`：无工具的System提示。Human由代码拼接业务权威状态和原会话时序转录，输出交接文本，
  不改写规则/资料库；续跑的业务适配器负责读取最新状态、重新组织System与当前阅读范围。
- 两份`parser_*.md`：无业务占位符，视觉模块直接读文件，Human另外附图片上下文/相邻原文与多模态图片。
  转写输出与 `ImageReading`、续表合并结果校验配套；不是业务Agent的`view_image`工具说明。

资料入库使用`evidence_ingestion*.md`，资料核验使用`evidence_subagent*.md`。
模型配置仍沿用`evidence_classification_model`及相应环境变量名称，它们供当前入库Agent使用，不代表存在独立分类步骤。

## 工具说明配置

`tool_descriptions.yaml` 中七个字符串分别描述 `search_documents`、`view_image`、`submit_rule_delta`、
`save_evidence_material`、`request_evidence_review`、`report_document`、`run_python`。通用检索/看图/沙箱由四类Agent复用，
其余分别由规则入库、资料入库、主审核使用。入参Field说明与Schema在 `app/tools/`、`app/models/` 中维护，
返回结果由工具实现渲染；只改YAML不会改变权限、参数个数、返回字段或读写行为。

Task34：`checks[].rule_ids` 关联一项事实涉及的全部规则；同一项非空去重并校验冻结规则，项间可以重用同一规则。
这些ID只用于代码关联、记录及返回标题，不注入子Agent Human，主审仍须在 `verification_request` 提供实际标准。
代码只拼“资料核验结果序号＋关联规则”标题和子正文，不重抄委派全文；不新增消息类型或共享事实缓存。
主审已有可见事实充分时无需委派，子审详细摘要充分时无需原文检索；实际采纳原文/看图才登记真实来源键。
System 管判断与取证策略，工具说明管能力/错误与 `code/print/status/result` 契约；轮次提醒沿用原时机。

例如扩充检索使用说明，应改`search_documents`对应块字符串；扩充入参则需同步
`app/tools/common_tools.py`中的输入模型、校验、工具实现与测试，不能仅在文字里承诺支持新参数。

检索工具在 `CommonTool.search_tool()` 内以 `string.Template.substitute` 注入两个 Top-K 配置值，再拼接实际授权范围；缓存的仍是 YAML 原始文本，不缓存跨会话的渲染结果。JSON 示例的普通花括号不会被解释为占位符；缺少变量会报错，不向模型发送未渲染模板。

Task32 D1/D2 暂时停用检索正文／来源 quote／查询组的字符裁剪，以及历史摘要工具返回的专用预算；旧判断留注释供恢复。最终命中片段与所选历史摘要完整返回，不等于返回完整文件或取消所有限制：Top-K、Rerank、去重、授权、图片保护、统一输入控制器、初始阅读窗口及 Trace 展示裁剪均保留。

普通工具异常和有真实调用 ID 的非法 JSON 由既有执行边界返回错误 ToolMessage，具体错误不搬进 System，不插入额外进度 Human。取消、不可配对的协议异常、硬轮次／输入窗口边界仍保持；错误不退还轮数，不自动续跑。参数拒绝与执行中失败分开说明，后者不能一律承诺未落盘。

## 独立报告模板

[audit_report_template.md](templates/audit_report_template.md) 单独维护报告结构和填写要求，是无业务占位符的静态 System 片段，不是已生成的报告正文。通过 `${report_markdown_contract}` 注入主 Agent 的 System；Human 和工具说明不重复模板。任务创建时固定此模板，批次与压缩续跑沿用快照。

主 Agent 通过 `report_document` 保存 Markdown，最终回复只说明处理情况；子 Agent 直接返回核验正文。报告初始化、草稿/最终校验和 MD 转 JSON 位于 `app/reports/markdown_report.py`，更换模板结构须同步调整解析器。`AgentAuditDecision` 仅为代码解析后的内部对象，不再注入模型。

主审核按“取得事实→增量保存已确认明细→处理冲突及缺口→依据最终明细归纳重点与总结”组织工作，不固定阶段轮数。允许同轮保存事项 A 并查询无依赖的事项 B，不能预写 B 尚未返回的结果。无需先完成总结/全部核验，也不为了增量而空写或凑判断。普通轮次仅沿用AI/Tool历史，不额外插入草稿进度Human；保留既有最后工具轮与无工具末轮提醒，不自动补轮。

`report_document`成功读取或完成文本保存后，在原ToolMessage的content首部返回当前草稿状态（已填写数、缺项或具体内容错误），后接read的完整草稿或write/edit的简短保存回执，不新增all/changed模式。首部不是草稿正文，不落盘、不用于edit定位，也不进入正式报告。其他工具结果不附加报告状态；既有末轮缺项提醒仍保留。状态随工具结果参与原有预算/压缩，不扩展通用控制器。write/edit同调用重放返回原回执，read读取当前草稿，不缓存读取结果。

只维护一份工作草稿：参数合法、替换唯一时原子保存，再返回内容校验反馈；格式或引用有误的当前稿可 `read/edit` 修正。参数错误、替换未命中/歧义、IO 失败不能视为保存成功。每批结束检查已填内容，最终完整校验通过才生成正式结果；不引入双版本、额外修复轮或完成工具。

v2 解析仅在规则集合与顺序完整、归属唯一时补齐遗漏的固定分类标题；自由正文中的已知内部引用按冻结快照转换为名称/正式明细节号，原始草稿与结构化键保留。未知编号不猜，真实文件名与登记引文不改写，来源授权仍严格校验。

### 模板编辑约束

- 使用 `$name` 或 `${name}` 声明占位符。
- 新增占位符后，必须在对应 `render_prompt()` 或工具描述的 `Template.substitute()` 调用中提供同名参数。
- 缺少参数会立即报错，不会把占位符原样发送给模型。
- 路由输出以 `BidRouteDecision` 的两字段 Schema 为准；Agent 不绑定最终 JSON Schema，工具参数仍严格校验。
- `tool_descriptions.yaml` 只修改模型看到的工具说明，不修改工具名或参数 Schema。
- 业务身份、来源对象和完整领域模型不应为了方便而重新塞入 LLM 输入。
- `${...}`之外的美元符号若经过 `render_prompt` 或工具描述模板渲染须写为 `$$`；直接读文件的静态parser模板不做此替换。
- 修改后重启服务刷新缓存；代码会缓存模板与工具说明，已创建任务还可能持有旧模板快照。
- 维护说明放在本README，工具配置用块字符串外的YAML注释；模板全文（包括HTML注释）可能进入模型。
