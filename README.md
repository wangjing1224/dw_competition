# 电力智能招标审核系统

本项目面向电力招投标材料，按招标规则逐项审核投标文件，并按需核验投标方支撑资料，最终生成可追溯的 JSON 审核结果和 Word 报告。

系统业务 ID 使用按类型独立递增的短格式，例如 `project-1`、`bidder-1`、
`audit-1` 和 `file-1`；索引片段使用 `chunk-文件号-章节号-单元号-切片号`，
有序解析结果的片段另带短版本后缀，避免重建索引后旧片段 ID 指向不同内容。

## 主流程

```text
招标文件夹 -> 文档解析 -> 招标依据索引 -> 逐文件规则 Agent -> RuleSet
投标方资料 -> 整批文档解析 -> Evidence Index -> 逐文件资料入库 Agent -> 双摘要与文件关联
投标测试集 -> 文档解析 -> 项目路由 -> 匹配文件统一建索引 -> 逐文件主/子 Agent -> Markdown报告 -> JSON -> Word
```

LangGraph 承载规则抽取 Agent、资料入库 Agent、主审核 Agent 和资料核验子 Agent。文件上传、解析和索引构建在图外完成；JSON 校验和 Word 渲染也在图外完成。

资料库按 bidder_id 独立入库，不绑定招标项目。一次审核任务可上传多份投标文件，每份文件分别选择唯一的已就绪规则库和资料库；每份文件独立路由和审核，一份文件输出一份报告。不匹配或不确定文件也输出理由，但不进入主 Agent；执行错误单独记录，不伪造成“人工复核”结论。

## 目录职责

```text
app/api          HTTP 接口
app/services     普通业务流程编排
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
app/reports      JSON 和 Word 输出
web              Ant Design Pro 规则库与投标方资料库工作台
configs          模型、检索和存储配置
data             运行时上传、解析、索引和输出
schemas          导出的 JSON Schema
tests            单元、契约、集成和端到端测试
```

## 环境和启动

项目使用 Python 3.11 和 uv：

```bash
uv sync
cp .env.example .env
uv run uvicorn app.main:app --reload
```

健康检查地址：`http://127.0.0.1:8000/health`

管理工作台（Node.js 22 或以上；后端保持在 8000 端口）：

```bash
cd web
npm ci
npm run dev
```

打开 `http://localhost:3000`。首页进入规则库，支持文件/文件夹导入、后台进度、
本地 Agent 执行轨迹、六类规则审阅、人工修订、来源原文/图片预览及 JSON/Markdown 导出。
投标方资料库支持独立批次导入、后台轨迹、逐文件双摘要和直接关系、人工修订及导出。
审核任务支持逐文件库配置、任务处理矩阵、主/子 Agent 执行轨迹、报告草稿、正式结果分析和逐文件导出。
没有登录系统，也不会请求模板的演示服务。
开发代理默认把 `/api/` 转到 `http://127.0.0.1:8000`，可通过启动环境变量
`API_PROXY_TARGET` 修改。生产部署使用 `npm run build` 的 `web/dist`，Web 服务器需配置
SPA 回退到 `index.html`，并将 `/api/` 反向代理到后端；开发代理不适用于生产静态文件。

规则库、资料库上传保存文件后即返回 202，随后在单进程后台解析和整理。重启会将未完成任务
标记为中断并保留已保存成果，不自动重复调用模型。当前不支持多进程任务调度，
部署时不要增加 Uvicorn workers；本地展示轨迹不依赖 LangSmith 开启。
人工编辑使用版本校验防止覆盖新修改。系统尚无鉴权，仅用于可信环境。

`app/web/` 是保留的早期静态演示，不适配当前审核接口；请使用上面的 `web/` 管理工作台。
更新后需重启后端加载新路由。数据库会迁移上一版审核绑定结构，保留已有规则库、资料库和原件，不需要删除 data。

服务启动只检查本地目录和 SQLite，不会主动请求模型。调用项目上传、
投标方资料上传或审核接口前，需要在 `.env` 中填写对应模型 URL 和 Key。

## 模型配置

- `.env`：保存各职责 Chat、Embedding 和 Rerank 的连接地址、Key，不提交 Git。
- `configs/model.yaml`：保存 Chat 平台、模型名、思考开关、温度、超时、重试、维度和批大小。
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

上传接口不变。解析和索引完成后，规则 Agent 串行处理每份招标文件，使用独立消息历史：
System 放稳定要求和启动时的完整规则视图，Human 放当前文件的有序正文、表格及图片路径。
提交结果以 ToolMessage 更新上下文，不反复改写 System；下一份文件使用最新规则库启动。

- `search_documents`：公共检索工具，规则侧按查询组回查当前文件及已完成的历史文件，不提前读取其他未来文件；补查不推进阅读游标。
- `view_image`：通过统一图片路径查看当前或历史文件中的已登记图片；由 `CommonTool` 提供。
- `submit_rule_delta`：一批 `add/merge/replace` 或项目简介更新；三个参数显式全为 `null` 时只读查看。

每条规则保存 `rule_id/provenance/category/requirement/priority/verification/reason/sources`。
`rule_id` 和 `provenance=ai_generated` 由系统赋值；来源只保存文件 ID、名称和路径。
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

### 投标方资料入库

一次上传一批资料，先统一解析并构建原文索引，再串行运行资料入库 Agent；不支持同一投标方完成后追加上传。
每份文件保存 `bid_summary`、`evidence_summary`、`relations`。关系为 `[自由文本描述, 历史file_id]`，不设类型枚举，
仅关联本批已完成的前序文件；代码可以按目标文件反查，不把有关联当作核验通过。
详细摘要按需保留真实图片路径，简洁摘要不放路径；历史详情和图片可以按需读取，不强制遍历全部资料。

三个工具为 `search_documents`、`view_image`、`save_evidence_material`。
保存工具的 `material`、`file_ids` 必须显式填写且恰好一项为 null：写入当前文件完整结果，或查看指定历史文件/`"all"`。
有变化才再次保存，不要求每个阅读批次重复提交；首次处理的文件必须有结果。保存失败保留进度，不伪装成人工核验结论。
结果在 `data/parsed/evidence/{bidder_id}.json`，执行进度在同目录 `{bidder_id}.progress.json`。
失败的完整上传批次可由 `BidderService.resume_bidder(bidder_id)` 恢复；未传完文件的批次不能按部分文件恢复成成功。

沿用 `EVIDENCE_CLASSIFICATION_MODEL_BASE_URL/API_KEY` 和 `evidence_classification_model` 配置名称，实际执行已改为入库 Agent。
`evidence_ingestion_max_steps` 按阅读批次计数；压缩续跑不清零，倒数第二轮提醒保存、最后一轮不绑定工具。
原文检索和看图与规则 Agent 共用 CommonTool，业务代码注入各自授权范围、默认范围和解析版本。

旧版 SQLite 的 bidders/files 仍绑定项目，启动时会提示旧结构并停止，不自动迁移或删除旧数据。
试跑新版前，请在 `configs/storage.yaml` 将 **data_root 和 database_path 一起**改为独立目录，例如
`data/ingestion-v23` 与 `data/ingestion-v23/app.db`；旧目录保留作参考。不要仅更换数据库却复用旧解析/索引目录。

### 投标审核与报告

一次任务可上传多份投标文件，每份文件独立绑定规则库和投标方资料库，也可批量应用同一组库。
创建时校验库状态、revision、解析快照与索引版本；缺少索引或索引过期不能直接放行。
提交后冻结所选规则、资料、解析版本和报告模板，后续编辑库不会改变正在执行任务的依据。

- 路由逐文件独立、有界并发，仅输出匹配状态与理由，不生成投标摘要。能放下时读全文，超长时只读连续完整结构前缀，并明确未读范围。
- `not_matched`、`uncertain` 不进入审核或投标索引，但仍进入 JSON 并生成“未进入审核”的 Word；模型报错不是 `uncertain`。
- 匹配文件独立建立 `bid-{task_id}-{file_id}` 原文索引并运行主 Agent，不等待其他文件。System 放完整规则和报告模板，Human 放当前文件原文/结构批次、简洁资料目录及已保存报告。
- 主 Agent 使用公共检索、看图、资料委派和报告工具。资料核验子 Agent 只看到核验要求、所选文件及一层关联文件的详细摘要，使用公共检索/看图，直接返回核验正文。
- 每份文件一份 Markdown 报告，通过 `report_document` 的 `read/write/edit` 维护；路径由代码绑定，模板单独放在 `configs/prompts/templates/audit_report_template.md`，不是工具参数。
- 报告一级标题由主 Agent 根据当前文件撰写，保存为 JSON 的 `report_title`，Word 直接使用，不额外调用模型。未进入主 Agent 的文件使用“文件名＋项目适用性说明”；旧结果没有标题时按文件名生成展示标题。
- 原文分批串行完成；同批压缩不推进阅读游标、不重置轮数。倒数第二轮提醒保存，最后一轮全部解绑工具。报告完整且原文读完才导出，不再机械补“步数上限，需人工复核”。

`configs/model.yaml` 的 `audit.file_concurrency` 默认 2，控制同进程各任务共用的文件流水线并发；
`audit.model_concurrency` 默认 3，限制同一进程、相同配置下审核链路实际模型请求。
父 Agent 等待工具或子 Agent 时不占模型名额。轮次仍由 `.env` 的 `BID_AGENT_MAX_STEPS`、
`EVIDENCE_AGENT_MAX_STEPS` 控制。路由、主 Agent、子 Agent 分别使用自己的模型连接配置，
审核不需要规则抽取/资料入库的 Key。

任务产物位于 `data/outputs/{task_id}/`：

- `batch_context.json`：本批次选中库、解析版本和模板快照，不含 Key。
- `contexts/{file_id}.json`、`parsed/{file_id}.json`：单文件执行上下文和实际解析快照。
- `drafts/{file_id}.md`：唯一报告草稿；同目录 `.state.json` 记录阅读位置、实际读取来源和子核验记录。
- `reports/{file_id}.json`：正式单文件结果与依据快照，schema_version=2.0。
- `audit_batch.json`：批次汇总，schema_version=3.0，逐文件保存绑定、结果和执行/导出错误。
- 逐文件 Word：由正式 JSON 确定性生成，不增加报告 LLM 或图节点；下载以接口登记 URL 为准。

来源只接受实际展示过的片段或实际打开的图片；没有来源不伪造引用。Word 只展示本文件结论及对应依据，不堆整库摘要。
单文件 JSON/Markdown 校验通过即发布；Word 失败保留正式结果。部分文件失败时批次为 partially_failed，
所有文件均有结果但 Word 失败为 completed_with_report_error，审核“不符合”不等于执行异常。
来源预览使用本次冻结解析版本，不承诺精确句子定位。所有 GET 只读，不触发模型、解析、建索引或报告重建。
新工作台暂不提供重跑、取消、人工编辑报告或重导出；`python -m app.reports` 只面向早期 audit_context.json 格式，不适用于新批次。

### Agent 收尾判定

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
迁移只改解析入口，文件之间仍按原顺序串行处理。

解析 JSON 增加有序 `sections[].blocks`，保留正文、表格、图片、页面来源和读取限制。
旧 `content/tables` 字段由新结构派生以兼容现有调用；早期无图片引用的旧 JSON 可以读取，但无法恢复
历史丢失的图片和原始交错顺序，需要重新解析。复杂表格使用 HTML 保留行列合并。

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
curl -X POST \
  http://127.0.0.1:8000/api/audit-tasks \
  -F 'display_name=设备采购审核' \
  -F 'client_request_id=00000000-0000-4000-8000-000000000001' \
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

测试与 Schema 导出：

```bash
uv run pytest
uv run python -m app.models.schema_export
```

普通测试全部使用临时数据和 Mock 模型，不请求外部服务。真实比赛数据 E2E：

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

运行时文件保存在 `data/`，包括 SQLite、上传文件、解析 JSON、NumPy 索引和
最终报告。当前 SQLite 仅用于本地比赛演示，不设计长期数据迁移和备份。
