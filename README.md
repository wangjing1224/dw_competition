# 招投标智能审核系统

本项目面向招投标材料，提供规则库、投标方资料库和审核任务工作台。系统按招标规则逐项审核投标文件，按需核验支撑资料，并生成结构化 JSON、正式 Markdown 阅读稿和 Word 报告。

当前为单进程、本地文件存储的辅助审阅系统，不提供登录鉴权，也不以模型结论替代人工审阅。以下说明对应当前实现；待验证方案不代表已上线能力。

## 当前功能

| 模块 | 已实现能力 |
| --- | --- |
| 规则库 | 文件/文件夹导入、后台抽取、六类规则审阅、人工新增与修订、解析原文及图片预览、JSON/Markdown 导出 |
| 投标方资料库 | 独立批次导入、逐文件简洁/详细摘要、文件间直接关系、人工修正、按 AI 整理/人工修正筛选及导出 |
| 审核任务 | 每份投标文件分别绑定规则库和资料库、任务队列、文件处理矩阵、独立路由与审核、批次 JSON 和逐文件结果导出 |
| 执行工作区 | 当前文件进度、流程图、固定能力节点的 Agent 图、默认展开的轨迹树、节点详情、执行记录及 Agent 历史回放 |
| 报告 | 草稿查看、正式结果总览与规则明细、已登记原文摘录、JSON/Markdown/Word 导出；未进入审核时单独展示项目适用性说明 |

规则库名称、资料库名称、审核任务名称各自在本类型内唯一，不同类型之间可同名；已有历史重名数据不会被自动改名。

系统业务 ID 使用按类型独立递增的短格式，例如 `project-1`、`bidder-1`、
`audit-1` 和 `file-1`；索引片段使用 `chunk-文件号-章节号-单元号-切片号`，
有序解析结果的片段另带短版本后缀，避免重建索引后旧片段 ID 指向不同内容。
业务页面优先展示名称、文件名和当前规则集内的序号，技术 ID 仍用于接口、路由、结构化 JSON 和诊断，不能因展示调整而更换关联键。

## 主流程

```text
招标文件夹 -> 文档解析 -> 招标依据索引 -> 逐文件规则 Agent -> RuleSet
投标方资料 -> 整批文档解析 -> Evidence Index -> 逐文件资料入库 Agent -> 双摘要与文件关联
投标文件   -> 逐文件解析与项目适用性判断
             ├─ 匹配：独立建索引 -> 主审核/资料核验 Agent -> 增量草稿 -> 最终校验
             │                                                   -> 正式 JSON -> Markdown/Word
             └─ 不匹配或不确定：不进入审核 Agent -> 项目适用性说明
```

LangGraph 承载规则抽取 Agent、资料入库 Agent、主审核 Agent 和资料核验子 Agent。上传、解析和索引构建由业务代码编排；报告的结构校验、正式阅读稿组装和 Word 渲染由代码完成，不再调用一个报告生成模型。

资料库按 bidder_id 独立入库，不绑定招标项目。一次审核任务可上传多份投标文件，每份文件分别选择唯一的已就绪规则库和资料库；每份文件独立路由和审核，一份文件输出一份报告。不匹配或不确定文件也输出理由，但不进入主 Agent；执行错误单独记录，不伪造成“人工复核”结论。

## 目录职责

```text
app/api          HTTP 接口
app/services     普通业务流程编排
app/audit        审核路由、会话及输入装载
app/storage      SQLite 元数据和本地文件
app/models       共享 Pydantic Schema
app/parsers      DOC/DOCX/XLSX/TXT/Markdown/PDF/图片解析及可选视觉辅助
app/llm          LangChain 模型适配
app/retrieval    本地向量和关键词检索
app/rules        逐文件规则编排、增量应用、规则视图和安全追踪
app/evidence     逐文件资料理解、双摘要持久化及关联查询
app/tools        规则/审核工具及 CommonTool 通用工具
app/agents       规则抽取、主审核和资料核验 Agent
app/graph        LangGraph State、路由和更新
app/reports      草稿维护、完整性校验、正式 Markdown 和 Word 输出
web              Ant Design Pro 规则库、资料库与审核工作台
configs          模型、检索和存储配置
data             运行时上传、解析、索引和输出
schemas          导出的 JSON Schema
tests            单元、契约、集成和端到端测试
```

## 环境和启动

### 后端

使用 Python 3.11 和 uv。在项目根目录安装依赖，首次使用时复制配置；已有 `.env` 不要覆盖：

```bash
uv sync
cp -n .env.example .env
```

在 `.env` 填好所需模型地址和 Key，再按下面的模型配置说明核对平台、型号与检索配置。启动开发后端：

```bash
uv run uvicorn app.main:app --reload
```

- 健康检查：`http://127.0.0.1:8000/health`
- 接口文档：`http://127.0.0.1:8000/docs`

服务启动只检查本地目录和 SQLite，不会主动请求模型。上传解析、规则抽取、资料整理和审核会按配置使用模型或检索服务，可能产生费用。

### 前端

使用 Node.js 22 或以上；从项目根目录打开另一个终端，后端保持在 8000 端口：

```bash
cd web
npm ci
npm run dev
```

打开 `http://localhost:3000`，首页为工作台，可进入规则库、投标方资料库和审核任务。
正式路由在 `web/config/routes.application.ts`；不使用模板登录页或演示服务。
开发代理默认把 `/api/` 转到 `http://127.0.0.1:8000`，可通过启动环境变量
`API_PROXY_TARGET` 修改。生产部署使用 `npm run build` 的 `web/dist`，Web 服务器需配置
SPA 回退到 `index.html`，并将 `/api/` 反向代理到后端；开发代理不适用于生产静态文件。

### 部署与更新边界

规则库、资料库和审核任务保存上传文件后即返回 202，随后在单进程后台处理。重启会将未完成任务
标记为中断并保留已保存成果，不自动重复调用模型。当前不支持多进程任务调度，
部署时不要增加 Uvicorn workers；本地展示轨迹不依赖 LangSmith 开启。
人工编辑使用版本校验防止覆盖新修改。系统尚无鉴权，仅用于可信环境，不能直接开放到不可信网络。

`app/web/` 是保留的早期静态演示，不适配当前审核接口；请使用上面的 `web/` 管理工作台。
后端代码、配置或 Prompt 更新后需要重启服务刷新缓存；先确认没有需要继续执行的任务。
已创建的审核任务保留自己的冻结快照，不因后续改库或改模板而重写。生产前端更新后需重新构建和部署静态文件。

常规更新不要删除 `data/`。数据库支持上一版审核绑定结构的定向迁移，但不是任意旧版本的通用迁移器。
若启动明确提示早期 `bidders/files` 仍绑定项目，则会停止且不自动改写旧数据；试跑新版需在
`configs/storage.yaml` 将 `data_root` 和 `database_path` 一起改为独立目录，例如
`data/ingestion-v23` 与 `data/ingestion-v23/app.db`，保留旧目录。不要只换数据库却复用旧解析/索引目录。
备份应在停止写入后同时保留数据库、原件、解析版本、索引和任务产物；当前没有内置备份恢复或跨进程任务恢复服务。

## 模型配置

- `.env`：保存各职责 Chat、Embedding 和 Rerank 的连接地址、Key，以及可选运行设置，不提交 Git。
- `configs/model.yaml`：保存 Chat 平台、模型名、思考开关、温度、超时、重试、维度、批大小及 Agent 轮次和审核并发配置。
- `configs/retrieval.yaml`：保存分段、候选数量和混合召回权重。
- `configs/storage.yaml`：保存数据目录、SQLite 路径和上传安全限制。
- `configs/prompts/`：保存各流程的 Prompt 模板和工具说明。

Chat 使用显式平台适配，共享 LangChain 的 Chat Completions 封装：

| `platform` | 典型服务地址 | `enable_thinking` 对应请求参数 |
| --- | --- | --- |
| `deepseek` | `https://api.deepseek.com` | `thinking.type=enabled/disabled` |
| `bailian` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `enable_thinking`；思考模式用流式接收并聚合完整回复 |
| `openai` | `https://api.openai.com/v1` | 已登记推理型号映射 `reasoning_effort`，普通型号不发送第三方参数 |

切换时同时修改各角色的 `platform`、`model` 及 `.env` 对应 URL/Key，不根据模型名猜平台。
OpenAI 可切换思考示例为 `gpt-5.1`，开为 `medium`、关为 `none`；不支持关闭的推理型号会明确报错，
未登记的推理型号需先核实能力。协议实现见 `app/llm/platforms.py`，
参数参考 [DS 官方](https://api-docs.deepseek.com/guides/thinking_mode/)、
[百炼](https://help.aliyun.com/zh/model-studio/deep-thinking)、
[OpenAI](https://developers.openai.com/api/docs/models/gpt-5.1)。不自动替换平台或偷偷打开思考。
第三方 Chat 网关协议需另行核实，不能套用 DS 官方参数。

Embedding 与 Rerank 各自支持 `platform: bailian` 或 `local`，可独立选择。
当前配置使用百炼原生文本向量与重排接口，型号为 `qwen3.7-text-embedding`
（1024维、每批16条）和 `qwen3.7-text-rerank`。

- 在 `.env` 填写 `EMBEDDING_BASE_URL/EMBEDDING_API_KEY` 与 `RERANK_BASE_URL/RERANK_API_KEY`。
  地址采用百炼控制台实际 API Host 加 `/api/v1`，示例见 `.env.example`，必须替换工作空间占位符。
  不要使用 Chat 兼容地址或追加具体 endpoint；Key、工作空间和地域须对应。
- 文档向量与查询向量分别使用 `text_type: document/query`，本地仍做L2归一化、余弦召回、
  关键词融合和索引存储。云端不会托管本项目的向量库。
- 云端请求按配置共享并发名额，对网络错误/429/部分5xx有限重试；401/403/402等直接报错。
  Rerank默认不降级，失败不会偷偷加载本地模型。`allow_degradation: true` 才允许保留初召回顺序。
- 原文、表格等候选会发送给模型服务，使用前确认材料允许出域。服务启动本身不请求模型；
  第一次入库/检索才实际计费，日志只保留请求标识、数量、耗时等诊断信息。

完整本地备选块保留在 `configs/model.yaml` 注释中。切换时替换对应模型块并同步 `.env` 后重启：
Embedding使用llama.cpp的OpenAI兼容接口，`text-embedding-v4`只是本地别名，不套用百炼同名限制，
也不发送 `dimensions/text_type`；Rerank保持进程内 `Qwen3-Reranker-0.6B`，首次检索才加载，
缓存位于 `models/`。`source: modelscope` 表示下载本地权重，不是云端推理；离线可用 `source: local`。

更换Embedding平台、地址、型号、维度或输入语义后必须使用新索引。不匹配的索引会明确拒绝，
不自动迁移或删除历史数据；本轮用新上传的测试材料走正常入库流程即可，**无需删除数据库**。
Rerank切换不改变向量索引。正文切片默认1024个本地近似Token、重叠160；表格和图片转写保持结构，
不等于接口长度上限，也不自动合并短段落。字段完整说明见 `configs/` 内注释。

协议参考：[百炼文本向量](https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api)、
[百炼文本重排](https://help.aliyun.com/zh/model-studio/text-rerank-api)。先跑离线HTTP契约测试
`uv run pytest tests/contract/test_bailian_retrieval.py`；填好真实连接后，再用少量脱敏材料
检查文档/查询向量、重排顺序及完整检索链。离线通过不等于真实API权限或业务效果已验证。

## 规则抽取与增量维护

解析和索引完成后，规则 Agent 串行处理每份招标文件，使用独立消息历史：
System 放稳定要求和启动时的完整规则视图，Human 放当前文件的有序正文、表格及图片路径。
提交结果以 ToolMessage 更新上下文，不反复改写 System；下一份文件使用最新规则库启动。

- `search_documents`：公共检索工具，规则侧按查询组回查当前文件及已完成的历史文件，不提前读取其他未来文件；补查不推进阅读游标。
- `view_image`：通过统一图片路径查看当前或历史文件中的已登记图片；由 `CommonTool` 提供。
- `submit_rule_delta`：一批 `add/merge/replace` 或项目简介更新；三个参数显式全为 `null` 时只读查看。

每条规则保存 `rule_id/provenance/category/requirement/priority/verification/reason/sources`。
模型抽取时，`rule_id` 和 `provenance=ai_generated` 由系统赋值；人工新增、修订分别标记为
`human_added`、`human_modified`。来源只保存文件 ID、名称和路径。
`merge` 追加要求并合并来源，`replace` 完整替换且校验来源，二者保留目标 ID；`reason` 始终覆盖为最新依据说明。
暂不设置 30 条上限，不自动将同类规则拼接压缩，数量和粒度通过后续人工标注验证。

每次成功提交立即原子写入 `data/parsed/rules/{project_id}.progress.json`，整批失败不更新规则状态。
所有文件处理完成才生成正式 `{project_id}.json`；进度及变更记录保留，不自动跨进程续跑。
旧版规则数据不兼容、不迁移，也不会自动删除；测试新流程需重新上传原始招标文件抽取。

`configs/model.yaml` 顶层 `rule_agent_max_steps` 配置每个阅读批次的规则 Agent 调用轮数，默认 8；同批压缩续跑不清零。
同层 `rule_limit: 10000` 配置规则库总条数上限。整批增量校验后、保存前，若总条数超过该值，
会调用 `app/rules/policies.py` 的 `handle_rule_limit_exceeded()`；目前只打印警告并继续保存全部规则，
不拒绝、截断或自动合并。后续超限策略从这个函数接入，当前将上限设大即可保持原有业务行为。
倒数第二轮提醒保存且仍绑定工具，最后一轮提醒后使用不绑定工具的模型收尾，上一轮工具全部回应后才进入下一轮。
沿用 `RULE_EXTRACTION_MODEL_BASE_URL/API_KEY` 和 `rule_extraction_model.model`；模型需支持原生工具调用及多模态工具消息。
没有自动切换模型，实际网关的识图和工具调用能力需要单独验证。

当前按有序结构装载：全文能放下就整份输入，否则按连续完整块分批，不截表、不跳过超大块。
每批使用最新规则视图；正常换批不额外调用摘要模型。多轮历史超出阈值时生成交接摘要，
保留当前批原文、已保存规则、工具提交状态和业务轮次，从外层重新 invoke 继续当前批。
进度文件同时记录批次、下一结构位置、批次/文件调用数及压缩诊断。全部批次成功后才完成文件；
单结构过大、固定输入过大或压缩失败会明确停止并保留成果，不静默裁剪，也不自动重跑失败批次。
开启 LangSmith 时记录独立文件会话和脱敏的模型/工具调用，保留文字与位置，不上传图片 Base64。
规则、资料入库、审核主/子 Agent 及审核路由共用 `app/llm/model_diagnostics.py`，在通用模型调用层记录诊断。
后端控制台检索 `agent_model_request` / `agent_model_response`，按 `call_id` 配对并发请求，`role` 区分调用角色；
业务侧补充项目/资料库/任务/文件、批次、轮次等身份，诊断同时进入本地执行轨迹的模型节点。
请求含输入估算及实际传入的输出额度，响应含服务端 Token 用量、
结束原因、耗时、工具参数长度和 JSON 语法错误位置。未返回的用量为 null；字符数不是 Token 数，
`arguments_source=reserialized` 表示长度来自重新序列化而非原始响应。无效参数只记统计，不新增记录其正文或推理文本。
截断/无效工具调用使用 WARNING；未获得响应时记录 `agent_model_call_failed`。这些诊断不改变重试、轮数或提交策略。
规则库工作台支持人工新增、修订和版本校验；审核侧已复用输入控制器。

## 投标方资料入库

一次上传一批资料，先统一解析并构建原文索引，再串行运行资料入库 Agent；不支持同一投标方完成后追加上传。
每份文件保存 `bid_summary`、`evidence_summary`、`relations`。关系为 `[自由文本描述, 历史file_id]`，不设类型枚举，
仅关联本批已完成的前序文件；代码可以按目标文件反查，不把有关联当作核验通过。
详细摘要按需保留真实图片路径，简洁摘要不放路径；历史详情和图片可以按需读取，不强制遍历全部资料。
资料的 `provenance` 只有 `ai_generated`（AI 整理）和 `human_modified`（人工修正），
没有人工新增资料条目的模式。人工修正只表示内容被修改，不代表材料已核验真实有效。

三个工具为 `search_documents`、`view_image`、`save_evidence_material`。
保存工具的 `material`、`file_ids` 必须显式填写且恰好一项为 null：写入当前文件完整结果，或查看指定历史文件/`"all"`。
有变化才再次保存，不要求每个阅读批次重复提交；首次处理的文件必须有结果。保存失败保留进度，不伪装成人工核验结论。
结果在 `data/parsed/evidence/{bidder_id}.json`，执行进度在同目录 `{bidder_id}.progress.json`。
失败的完整上传批次可由 `BidderService.resume_bidder(bidder_id)` 恢复；未传完文件的批次不能按部分文件恢复成成功。

沿用 `EVIDENCE_CLASSIFICATION_MODEL_BASE_URL/API_KEY` 和 `evidence_classification_model` 配置名称，实际执行已改为入库 Agent。
`evidence_ingestion_max_steps` 按阅读批次计数；压缩续跑不清零，倒数第二轮提醒保存、最后一轮不绑定工具。
原文检索和看图与规则 Agent 共用 CommonTool，业务代码注入各自授权范围、默认范围和解析版本。

## 投标审核与报告

一次任务可上传多份投标文件，每份文件独立绑定规则库和投标方资料库，也可批量应用同一组库。
创建时校验库状态、revision、解析快照与索引版本；缺少索引或索引过期不能直接放行。
提交后冻结所选规则、资料、解析版本和报告模板，后续编辑库不会改变正在执行任务的依据。

- 路由逐文件独立、有界并发，仅输出匹配状态与理由，不生成投标摘要。能放下时读全文，超长时只读连续完整结构前缀，并明确未读范围。
- `not_matched`、`uncertain` 不进入审核或投标索引，但仍保存正式 JSON，并生成项目适用性说明的 Markdown/Word；模型报错不是 `uncertain`。
- 匹配文件独立建立 `bid-{task_id}-{file_id}` 原文索引并运行主 Agent，不等待其他文件。System 放完整规则和报告模板，Human 放当前文件原文/结构批次、简洁资料目录及已保存报告。
- 主 Agent 使用公共检索、看图、资料委派和报告工具。资料核验子 Agent 只看到核验要求、所选文件及一层关联文件的详细摘要，使用公共检索/看图，直接返回核验正文。
- 每份文件一份 Markdown 报告，通过 `report_document` 的 `read/write/edit` 维护；路径由代码绑定，模板单独放在 `configs/prompts/templates/audit_report_template.md`，不是工具参数。
- 报告一级标题由主 Agent 根据当前文件撰写，保存为 JSON 的 `report_title`，Word 直接使用，不额外调用模型。未进入主 Agent 的文件使用“文件名＋项目适用性说明”；旧结果没有标题时按文件名生成展示标题。
- 原文分批串行完成；同批压缩不推进阅读游标、不重置轮数。倒数第二轮提醒保存，最后一轮全部解绑工具。报告完整且原文读完才导出，不再机械补“步数上限，需人工复核”。

`configs/model.yaml` 的 `audit.file_concurrency` 默认 2，控制同进程各任务共用的文件流水线并发；
`audit.model_concurrency` 默认 3，限制同一进程、相同配置下审核链路实际模型请求。
父 Agent 等待工具或子 Agent 时不占模型名额。主/子 Agent 轮次分别由同文件中的
`audit.bid_agent_max_steps`、`audit.evidence_agent_max_steps` 控制。路由、主 Agent、子 Agent 分别使用自己的模型连接配置，
审核不需要规则抽取/资料入库的 Key。

### 草稿、工具反馈与正式报告

主 Agent 只维护一份内部 Markdown 草稿，使用 `report_document` 的 `read/write/edit`：

- `read` 返回当前草稿；`write/edit` 返回保存回执，不支持 `all/changed` 返回模式。
- 成功读取或保存后，在工具返回的 `content` 首部附上当前草稿状态，包括已填写数量、缺项或校验问题。这是 `ToolMessage`，不是每轮额外插入的 System/Human 消息，也不会写入报告正文。
- 草稿内容或来源校验有问题时，可以先保存文本并反馈错误，后续继续修订；工具参数错误、精确替换不唯一或写盘失败不算成功保存。只有一份草稿，不额外维护“待修版本”和“有效版本”。
- 最终仍须完成全部冻结规则、必填内容和来源校验。轮次耗尽、模型或工具异常时保留已有草稿与错误，不将不完整报告发布为正式结果，也不补造“人工复核”来凑齐规则。

普通工具轮保持 `AI → Tool` 循环；仅在最后工具轮和无工具收尾轮使用现有 Human 提醒。
当前 Prompt 要求边核验边保存已确认内容，不等最后一次性生成整稿；这不保证模型每次都按预期执行。

正式报告与内部草稿分开：

| 内容 | 来源与处理方式 |
| --- | --- |
| 报告标题、总体总结、重点问题归纳、逐条实际发现/判断说明/复核建议 | 模型填写并落入草稿，代码按契约解析和校验 |
| 审核概况、规则要求、章节编号、结论统计 | 代码依据任务和冻结快照组装，不让模型重抄 |
| 重点问题中的处理建议 | 按重点关联的规则复用逐条建议，不再维护一份重复建议清单 |
| 原文摘录与位置 | 只使用实际读取并登记的来源；没有记录时不推测页码或编造引文 |
| 正文中的内部 ID | 已知规则 ID 转为明细节号，文件/库/任务 ID 转为冻结名称；结构化关联键保留，真实文件名、业务编号和登记引文不改写 |

进入规则审核的正式阅读稿固定为四部分：**审核概况 → 结果总览 → 重点问题与处理建议 → 审核明细及原文摘录**。
规则结论统一为“符合、不符合、人工复核”，总体按“不符合 → 人工复核 → 符合”汇总；已确认条件不适用而无需响应的条目归符合并解释依据。执行失败不属于业务结论，“一致性与风险”仍是规则类别。旧四分类测试结果不做兼容或迁移，历史文件不自动改写。
未进入规则审核的文件只输出概况和项目适用性说明，不生成普通规则结论或统计。
内部草稿和 JSON 可以保留关联 ID；正式 Markdown/Word 不以 `rule-xxx` 等技术编号作为阅读标签。
字段约定见 [报告模板](configs/prompts/templates/audit_report_template.md)，消息与工具边界见 [Prompt 配置说明](configs/prompts/README.md)。

### 执行可视化

- 流程图展示文件的解析、适用性判断、索引、审核和报告输出路径；未走分支与实际执行分开显示。
- Agent 图使用固定能力节点，依据已加载轨迹更新状态、高亮和调用次数，不为每次工具调用新增一个能力节点。
- 轨迹树展示逐次真实调用，默认展开，可全部收起；长参数、可见输出和技术信息支持折叠及内部滚动。
- 历史回放依据已结束会话的调用时间重现节点状态与累计次数，不重新执行模型。节点详情仍是最新已保存记录，不是任意历史时刻的内部内容快照。
- 页面先显示可读的失败原因；原始异常和技术 ID 收在技术详情中。执行失败、未进入审核与业务结论“不符合”是不同状态。

### 产物与导出

任务产物位于 `data/outputs/{task_id}/`：

- `batch_context.json`：本批次选中库、解析版本和模板快照，不含 Key。
- `contexts/{file_id}.json`、`parsed/{file_id}.json`：单文件执行上下文和实际解析快照。
- `drafts/{file_id}.md`：唯一报告草稿；同目录 `.state.json` 记录阅读位置、实际读取来源和子核验记录。
- `reports/{file_id}.json`：正式单文件结果与依据快照，schema_version=2.0。
- `reports/{file_id}.md`：由正式结果组装的阅读稿，不是 `drafts/` 下的内部草稿。
- `audit_batch.json`：批次汇总，schema_version=3.0，逐文件保存绑定、结果和执行/导出错误。
- 逐文件 Word：由正式 JSON 确定性生成，不增加报告 LLM 或图节点；下载以接口登记 URL 为准。

来源只接受实际展示过的片段或实际打开的图片；没有来源不伪造引用。Word 只展示本文件结论及对应依据，不堆整库摘要。
最终校验通过后先保存正式 JSON，再生成 Markdown 和 Word；导出失败保留已生成的有效结果。
Markdown 生成失败时不继续生成 Word。部分文件执行失败时批次为 `partially_failed`，
所有文件均有结果但存在导出失败时为 `completed_with_report_error`，审核“不符合”不等于执行异常。
来源预览使用本次冻结解析版本，不承诺精确句子定位。所有 GET 只读，不触发模型、解析、建索引或报告重建。
新工作台暂不提供重跑、取消、人工编辑报告或重导出；`python -m app.reports` 只面向早期 audit_context.json 格式，不适用于新批次。

### Agent 收尾判定

当前默认预算如下；实际运行以生效配置为准：

| Agent | 配置项 | 默认模型轮数 |
| --- | --- | --- |
| 规则抽取 | `configs/model.yaml` 的 `rule_agent_max_steps` | 8 |
| 资料入库 | `configs/model.yaml` 的 `evidence_ingestion_max_steps` | 8 |
| 主审核 | `configs/model.yaml` 的 `audit.bid_agent_max_steps` | 8 |
| 资料核验子 Agent | `configs/model.yaml` 的 `audit.evidence_agent_max_steps` | 3 |

修改 YAML 后重启后端生效。旧 `.env` 或系统环境中的 `BID_AGENT_MAX_STEPS`、
`EVIDENCE_AGENT_MAX_STEPS` 不再读取，可删除；如曾自定义轮次，请将值迁入上述 YAML 字段。

轮数指一个阅读批次/会话的模型调用预算，不是工具调用次数。主审核 8 轮中前 7 轮可调用工具，
第 8 轮只作无工具回复；模型可以提前结束，不要求跑满。一轮可以发出多个工具调用，子 Agent 的模型调用不逐次扣减主 Agent 轮数。
同批压缩续跑不清零；上下文容量、单次输出额度和模型轮数是不同限制。

规则、资料入库和主审核 Agent 用简短普通文本说明本批处理情况，不要求固定结束语，也不增加完成工具或收尾 JSON。
模型正常停止、无待执行或无法解析的工具调用，并返回非空文本后，才结束本次会话；输出截断和未解决的写入错误仍阻断推进。
规则允许没有新要求的文件不提交；资料每份文件须已有保存结果，无变化的后续批次不必重复保存；审核最终仍校验已保存报告的完整性和来源。
这些检查只验证执行及产物状态，不对收尾文本做关键词或语义判定，也不保证内容没有遗漏；未完成原因仍须在回复中如实说明。
轮数、末轮不绑定工具、分批和压缩策略不变。

### 模型输入预算

规则、资料入库、路由、主审核及资料核验模型使用 `configs/model.yaml` 中的 `context_window_tokens`、`compaction_trigger_ratio`（默认 0.90）
和 `tokenizer`（`DeepSeek` 或 `utf8`）。这些角色取消固定 `max_output_tokens`，发送前按剩余额度和已登记服务上限计算；解析视觉辅助模型仍使用固定输出配置。
控制器核算 System、实际工具/输出约束、Human、AI/Tool 历史四类内容，计入真实图片但不把 Base64 当文字。
相同请求复用计数；有可靠服务用量时补计新增历史，重建消息或更换绑定后重新本地核算。
输入达到阈值才按业务策略处理，不把 Token 判断放在动作路由中。

接口输出额度从预留窗口推导：默认约 10% 中留窗口的 1% 给计数误差，再受实际剩余空间与服务输出上限约束。
压缩调用可越过业务软阈值，但输入、输出和余量仍须满足硬容量。能力表在 `app/llm/model_capabilities.py`，
目前登记 DS 官方 Flash、OpenAI GPT-5.1 和百炼北京 Qwen3.6-Plus/Qwen-Plus 的文本上限；
其他型号、地域和网关需先核实并登记，不能凭同名模型沿用官方上限。
DS 图片计数采用[官方单图上界](https://api-docs.deepseek.com/guides/vision/)；其他平台的图片预算未标定时明确阻塞。
Token 计数与实际发送使用同一个平台消息转换：DS 思考+工具模式回传并计入 `reasoning_content`，
取消工具的收尾不回传该字段；百炼已登记的 `preserve_thinking` 型号按协议回传。
OpenAI 不把隐藏的推理输出 Token 数直接累加成下一轮输入。平台/绑定变化会使旧计数基准失效。
分词器仍由 `tokenizer` 独立选择，不随平台自动切换；非 DS 模型暂用 `utf8` 保守估算。

`DeepSeek` 使用[官方分词资源](https://api-docs.deepseek.com/quick_start/token_usage/)，固定在本地缓存，
校验指纹并在进程中复用，不加载生成模型、不在请求时下载。首次准备资源：

```bash
curl -fL https://cdn.deepseek.com/api-docs/deepseek_v4_tokenizer.zip -o /tmp/deepseek_v4_tokenizer.zip
unzip -n /tmp/deepseek_v4_tokenizer.zip 'deepseek_v4_tokenizer/tokenizer*.json' -d models/tokenizers
```

分词资源位于 `models/tokenizers/deepseek_v4_tokenizer/`，不提交 Git；缺失或指纹不匹配时告警并回退 UTF-8 字节估算。
文本估算与服务端真实 Token 不完全等同，UTF-8 兜底尤其保守；实际图片缺少计数能力则明确阻塞。
本地小窗口 Mock 已覆盖分批、交接、失败保留与最终轮行为；真实接口的计数偏差、压缩质量仍需单独验证。

## 公共解析与视觉辅助

规则、资料入库及审核 Agent 均已接入有序解析结构、通用检索/看图工具和输入预算控制器。
三条业务解析路径统一调用异步 `prepare_document()`：先做原生解析，再按配置进行视觉辅助。
原同步公开入口 `parse_document()` 已删除；各格式解析器内部的 `.parse()` 仍保留。
统一解析入口不改变各业务的文件调度策略；审核仍按上述文件流水线并发配置运行。

解析 JSON 增加有序 `sections[].blocks`，保留正文、表格、图片、页面来源和读取限制。
旧 `content/tables` 字段由新结构派生以兼容现有调用；早期无图片引用的旧 JSON 可以读取，但无法恢复
历史丢失的图片和原始交错顺序，需要重新解析。复杂表格使用 HTML 保留行列合并。

DOCX 的 OLE/Package 嵌入附件按实际内容识别，不按 `.bin` 后缀猜格式；支持递归读取
嵌入的 DOCX、XLSX、旧版 XLS，以及 ZIP/RAR 内的这些文件。附件在原文位置展开，
章节保留父文件、附件和工作表/章节的来源链，图片与检索仍归属上传的父文件，不新增业务文件 ID。
只读附件正文，不执行宏、不自动下载外链、不把 OLE 预览图标当正文图片。
单个附件损坏、加密或格式不支持时，保留明确的未展开说明，继续解析其余正文与附件。

新增 Python 依赖由 `uv sync` 安装；压缩 RAR 还需系统解压器，本机可用 `bsdtar`，
无 `bsdtar` 时由 `rarfile` 使用其他已安装的解压器（见 [RAR 后端说明](https://rarfile.readthedocs.io/index.html)）。
`parsing` 下的 `max_embedded_depth`、`max_embedded_files`、`max_embedded_bytes`
分别限制嵌套层数、展开对象数及累计字节量，
OLE 包装与压缩容器也计入；这些是异常文件资源保护，不裁剪模型上下文或检索正文，超限会明确提示。
旧版 XLS 仅支持保存的单元格值和合并区域，不重算公式，也不展开其中的图形或子附件；
此类能力边界会保留在解析警告中。此变更不新增顶层 `.xls`/压缩包上传入口。
旧解析快照不会自动补入附件内容，需重新解析；规则、资料摘要及索引也不会自动重建。

产物位于 `data/parsed/{file_id}.json` 和 `data/parsed/{file_id}/`：

- `document.md`：文件内的最新 Markdown 副本；直接预览请打开 `data/parsed/{file_id}.md`，其图片链接可按相对路径正常打开。
- `images/`：文件内递增短名（如 `1.png`）；内部 `assets.json` 按内容哈希去重，旧编号不覆盖。JSON、Markdown（包括表格 HTML）和页面回查图统一使用 `{file_id}/images/1.png`，以 `data/parsed` 为资源根目录，不随版本目录改写。图片读取仍校验当前文件范围。
- `versions/`：不可变的 JSON/Markdown 快照；入口 JSON 的 `markdown_path` 指向同版本内容。
- `checkpoints/`、`vision-cache/`：原生解析及逐项处理检查点、成功视觉结果缓存，不保存 Base64。失败诊断位于 `checkpoints/diagnostics/`，包含页码、阶段和安全错误原因。

仍使用 `images/1.png` 等文件内路径的旧解析 JSON 需要重新解析，读取时会明确提示。
重新解析会复用已登记的图片（包括历史哈希文件名），生成带文件身份的新路径和快照，不改写旧版本文件。

视觉辅助独立配置，不使用主 Agent 的 Key。在 `.env` 填写：

```dotenv
PARSER_VISION_MODEL_BASE_URL=https://your-provider.example/v1
PARSER_VISION_MODEL_API_KEY=your-key
PARSER_VISION_MODEL_NAME=实际网关提供的多模态模型ID
```

`configs/model.yaml` 中的 `parser_vision_model` 控制生成参数，`parsing` 控制图片/PDF
资源限制；默认关闭自动视觉调用。配置目标为 DS V4.1 Flash，但模型 ID 按实际端点填写，
不能假设平台使用相同命名。没有视觉配置仍可做原生解析；需要识图的范围会标记未完成，
不会静默借用其他模型配置。

`parsing.vision_enabled` 是调用许可开关，不是让模型决定是否启动；普通图片逐张串行，
随图发送已知来源与邻近原文，续表每次比较一个相邻页边界的文字和两张回查图。
PDF 内容图片按原像素自适应提高区域渲染精度，保留页面遮盖/裁切语义；
`pdf_render_dpi` 是基础精度，`pdf_render_max_dpi` 是自适应精度上限，仍受像素和发送尺寸限制。

对已上传文件独立验证（会更新该文件解析产物，不重新生成规则或索引）：

```bash
uv run python -m app.parsers file-42
# 显式允许视觉调用，可能产生费用；只对必要图片/续表候选处理。
uv run python -m app.parsers file-42 --vision
# 显式关闭视觉；不传任一开关时跟随 YAML 配置。
uv run python -m app.parsers file-42 --no-vision
```

不要在正在审核同一文件时重解析。生成新解析版本后，旧索引不会自动更新。
本地测试使用合成文件和 Mock 视觉响应，不向外部服务发送真实资料；真实网关的识图效果
和图片工具消息支持仍需联调。`image_messages.py` 提供安全图片工具消息构造，规则 Agent 通过 `CommonTool` 复用。
解析视觉调用默认关闭 LangSmith Trace 采集，避免 Base64 图片和原文被第三方采集。

边界：PDF 的复杂排版与表格恢复不是无损保证；续表逐相邻页处理，不再限制整表最多三页。
模型只输出局部边界决定，代码保留原文单元格并拼接；复杂跨行/跨列、关系不确定或校验失败时
保留各页原表、图片和明确限制。图例配对仍可能出错，不能把视觉读取成功当作事实核实。
DOCX 浮动对象按 XML 锚点保留并提示阅读顺序限制；
无法解码的 WMF 等格式明确提示回查，未实现通用 Office 渲染兜底。XLSX 公式保留表达式，
不把未计算结果假装成有效数值。图片尚未转写、转写成功与业务审核通过是不同状态。

PDF 依赖 PyMuPDF；发布/分发项目前应核对其上游 AGPL/商业许可选项，见
[PyMuPDF 许可说明](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright)。

## API

上传招标文件，保存材料后返回 202，在后台生成 RuleSet：

```bash
request_id="$(uuidgen)"
curl -X POST http://127.0.0.1:8000/api/projects \
  -F "client_request_id=$request_id" \
  -F 'display_name=示例招标规则库' \
  -F 'files=@/path/to/招标文件.docx' \
  -F 'relative_paths=示例招标文件/招标文件.docx'
```

保存返回的 `project_id`，通过 `GET /api/projects/{project_id}/progress` 查询处理进度。
同一次提交重试必须复用原 `request_id`、名称和文件；不要重新运行 `uuidgen`。
需要确认是否已受理时，先查询 `GET /api/projects?client_request_id={request_id}`。

上传一家投标方的支撑资料，接收后返回 202 并后台整理：

```bash
request_id="$(uuidgen)"
curl -X POST http://127.0.0.1:8000/api/bidders \
  -F "client_request_id=$request_id" \
  -F 'display_name=示例投标方证明资料' \
  -F 'files=@/path/to/合同.docx' \
  -F 'relative_paths=1.业绩文件/合同.docx'
```

保存返回的 `bidder_id`，分别查询独立资料库详情及入库进度：

```bash
curl http://127.0.0.1:8000/api/bidders/{bidder_id}
curl http://127.0.0.1:8000/api/bidders/{bidder_id}/progress
```

同一次上传重试复用原 `request_id`、名称和文件，已接收返回 200 且不重复排队。
响应丢失时先查询 `GET /api/bidders?client_request_id={request_id}`；失败批次不自动重跑。
两类库各自使用一个 `files/{file_id}/preview` 接口：默认返回 Markdown 和登记图片清单，
图片使用清单内带 `image_name` / `version` 的 URL 按需读取，无须拼接磁盘路径。

创建审核任务（revision 先从所选库详情读取；每份文件一个 assignments 条目）：

```bash
request_id="$(uuidgen)"
curl -X POST \
  http://127.0.0.1:8000/api/audit-tasks \
  -F 'display_name=设备采购审核' \
  -F "client_request_id=$request_id" \
  -F 'assignments=[{"upload_index":0,"project_id":"project-1","bidder_id":"bidder-1","project_revision":"替换为规则库revision","bidder_revision":"替换为资料库revision"}]' \
  -F 'files=@/path/to/投标文件.docx' \
  -F 'relative_paths=投标文件测试集/投标文件.docx'
```

查询任务：

```bash
curl http://127.0.0.1:8000/api/audit-tasks/{task_id}
```

同一接口上传多个文件时，重复传递 `files` 和 `relative_paths`，两者顺序及数量
必须一致，assignments 的 upload_index 从 0 连续覆盖每个文件。每次新提交使用新的 UUID；
同次重试保持请求ID和内容不变，响应不明时先查询 `GET /api/audit-tasks?client_request_id=...`。
规则库、资料库与审核上传均在接收后返回，耗时处理在进程内后台执行。
审核详情返回 task/files；进度、轨迹、逐文件报告及导出分别使用 `/progress`、`/trace/{span_id}`、
`/files/{file_id}`、`/outputs?format=json|md|word&file_id=...`。省略 file_id 只能下载批次 JSON。

完整接口以运行服务的 `/docs` 和 `app/api/` 为准；读取、预览和下载接口不会触发重新审核。

## 测试与验证

后端离线测试（项目根目录；不启用真实模型 E2E）：

```bash
RUN_MODEL_E2E=0 uv run pytest
```

普通测试使用临时数据和 Mock 模型，不请求外部服务。前端测试与类型检查不需要启动项目：

```bash
cd web
npm test
npm run tsc
```

需要同步共享 Schema 时，在项目根目录执行（会更新 `schemas/`）：

```bash
uv run python -m app.models.schema_export
```

使用本地比赛材料的 Mock 全链路测试：

```bash
uv run pytest tests/e2e/test_mock_dataset.py -v -s
```

上述命令读取根目录 `test-datas/`，验证 SQLite、真实解析和索引、新规则/资料入库、两字段路由、主 Agent 工具保存报告及 JSON/Word 导出，仅外部模型使用 Mock。
`tests/integration/test_audit_multimodal.py` 另覆盖资料委派、原文检索、真实图片消息与来源导出；它验证流程与隔离，不代表真实模型识图或审核准确率已验收。

真实比赛数据全链路烟测（会调用规则/资料入库、路由及审核模型，需要可用的检索服务；请先配置隔离的数据目录，不使用已有业务目录）：

```bash
RUN_MODEL_E2E=1 \
COMPETITION_DATASET_ROOT=/path/to/招标文件资料 \
uv run pytest tests/e2e/test_competition_dataset.py -v
```

审核轮询超时默认1800秒，可用 `MODEL_E2E_TIMEOUT` 调整。该测试检查完整结果与报告，不代表与人工意见对照的效果验收。

`COMPETITION_DATASET_ROOT` 下应包含：

```text
示例招标文件/
投标方资料/
投标文件测试集/
```
