# 电力智能招标审核系统

本项目面向电力招投标材料，按招标规则逐项审核投标文件，并按需核验投标方支撑资料，最终生成可追溯的 JSON 审核结果和 Word 报告。

系统业务 ID 使用按类型独立递增的短格式，例如 `project-1`、`bidder-1`、
`audit-1` 和 `file-1`；索引片段使用 `chunk-文件号-章节号-单元号-切片号`，
减少日志、Prompt 和观测页面中的冗余字符。

## 主流程

```text
招标文件夹 -> 文档解析 -> LLM 抽取 RuleSet -> 招标依据索引
投标方资料 -> 文档解析 -> LLM 资料分类 -> Evidence Index
投标测试集 -> 文档解析 -> Bid Index -> 主/子 Agent 审核 -> JSON -> Word
```

LangGraph 只承载审核阶段的主审核 Agent 和资料核验子 Agent。文件上传、解析、规则抽取、资料分类和索引构建在图外完成；JSON 校验和 Word 渲染也在图外完成。

## 目录职责

```text
app/api          HTTP 接口
app/services     普通业务流程编排
app/storage      SQLite 元数据和本地文件
app/models       共享 Pydantic Schema
app/parsers      DOC/DOCX/XLSX/TXT/Markdown/PDF/图片解析及可选视觉辅助
app/llm          LangChain 模型适配
app/retrieval    本地向量和关键词检索
app/rules        规则抽取、合并和确定性判断
app/evidence     投标方资料分类
app/tools        主/子 Agent BaseTool
app/agents       主审核和资料核验 Agent
app/graph        LangGraph State、路由和更新
app/reports      JSON 和 Word 输出
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

演示前端：

```text
http://127.0.0.1:8000/web/
```

- 系统总览：展示处理边界和审核链路。
- 审核操作：选择三个文件夹，依次创建 Project、Bidder 和 AuditTask。
- 任务观测：自动轮询后台阶段，展示文件级结论、原始 JSON，并下载输出。

服务启动只检查本地目录和 SQLite，不会主动请求模型。调用项目上传、
投标方资料上传或审核接口前，需要在 `.env` 中填写对应模型 URL 和 Key。

## 模型配置

- `.env`：保存四类 Chat 模型和 Embedding 的连接地址、Key，不提交 Git。
- `configs/model.yaml`：保存模型名、温度、超时、重试、维度和批大小。
- `configs/retrieval.yaml`：保存分段、候选数量和混合召回权重。
- `configs/storage.yaml`：保存数据目录、SQLite 路径和上传安全限制。
- `configs/prompts/`：保存四套 Prompt 模板和五个 Tool 的模型可见说明。

Chat 和 Embedding 使用 LangChain 的 OpenAI 兼容封装。首期可使用阿里百炼；
后续切换本地服务时，修改对应 URL、Key 和模型名即可。Rerank 使用进程内
`sentence-transformers` CrossEncoder，默认加载 `Qwen3-Reranker-0.6B`：
Mac 优先使用 MPS，Linux 优先使用可用的 NPU/CUDA，设备加载或推理失败时
自动回退 CPU。模型在第一次检索时通过魔塔社区下载并复用本地缓存，不影响
后端启动速度。离线服务器可提前下载模型，并把 `model` 改为本地目录、
`source` 改为 `local`。

Rerank 模型固定下载到项目根目录的
`models/Qwen/Qwen3-Reranker-0.6B`，`models/` 已加入 `.gitignore`，
不会使用或依赖可能被清理的 `~/.cache`。

当前 Embedding 使用本地 llama.cpp 部署的 `Qwen3-Embedding-0.6B`，按模型
原生 1024 维输出进行校验。客户端不会发送 llama.cpp 未支持的
`dimensions` 参数。

Embedding 模型、URL 或维度变化后，停止服务并删除 `data/indexes/` 和
`data/app.db`，再依次重新调用项目、投标方和审核任务接口。

## 公共解析与视觉辅助

当前先实现解析能力，**尚未改造规则/资料 Agent、检索消费逻辑或输入预算控制器**。
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
和图片工具消息支持仍需联调。`image_messages.py` 提供安全图片工具消息构造，暂未绑定业务 Agent。
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

上传招标文件并同步生成 RuleSet：

```bash
curl -X POST http://127.0.0.1:8000/api/projects \
  -F 'files=@/path/to/招标文件.docx' \
  -F 'relative_paths=示例招标文件/招标文件.docx'
```

上传一家投标方的支撑资料：

```bash
curl -X POST http://127.0.0.1:8000/api/projects/{project_id}/bidders \
  -F 'files=@/path/to/合同.docx' \
  -F 'relative_paths=1.业绩文件/合同.docx'
```

创建审核任务：

```bash
curl -X POST \
  http://127.0.0.1:8000/api/projects/{project_id}/bidders/{bidder_id}/audit-tasks \
  -F 'files=@/path/to/投标文件.docx' \
  -F 'relative_paths=投标文件测试集/投标文件.docx'
```

查询任务：

```bash
curl http://127.0.0.1:8000/api/audit-tasks/{task_id}
```

同一接口上传多个文件时，重复传递 `files` 和 `relative_paths`，两者顺序及数量
必须一致。项目和投标方预处理接口当前同步返回；审核任务接口保存原始文件后
立即返回 `task_id`，解析、索引、LangGraph 和报告输出在进程内后台执行。

测试与 Schema 导出：

```bash
uv run pytest
uv run python -m app.models.schema_export
```

普通测试全部使用临时数据和 Mock 模型，不请求外部服务。真实比赛数据 E2E：

```bash
uv run pytest tests/e2e/test_mock_dataset.py -v -s
```

上述命令读取根目录 `test-datas/`，执行真实 SQLite、文件解析、索引、后台审核、
JSON 和 Word 输出，仅将外部模型调用替换为确定性 Mock。适合在上传比赛材料前
验证本地代码链路。

真实比赛数据 E2E：

```bash
RUN_MODEL_E2E=1 \
COMPETITION_DATASET_ROOT=/path/to/招标文件资料 \
uv run pytest tests/e2e/test_competition_dataset.py -v
```

`COMPETITION_DATASET_ROOT` 下应包含：

```text
示例招标文件/
投标方资料/
投标文件测试集/
```

运行时文件保存在 `data/`，包括 SQLite、上传文件、解析 JSON、NumPy 索引和
最终报告。当前 SQLite 仅用于本地比赛演示，不设计长期数据迁移和备份。
