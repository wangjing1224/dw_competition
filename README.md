# 电力智能招标审核系统

本项目面向电力招投标材料，按招标规则逐项审核投标文件，并按需核验投标方支撑资料，最终生成可追溯的 JSON 审核结果和 Word 报告。

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
app/parsers      DOCX/XLSX/TXT 解析
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

- `.env`：保存六类模型连接地址和 Key，不提交 Git。
- `configs/model.yaml`：保存模型名、温度、超时、重试、维度和批大小。
- `configs/retrieval.yaml`：保存分段、候选数量和混合召回权重。
- `configs/storage.yaml`：保存数据目录、SQLite 路径和上传安全限制。

Chat 和 Embedding 使用 LangChain 的 OpenAI 兼容封装。首期可使用阿里百炼；
后续切换本地服务时，修改对应 URL、Key 和模型名即可。Rerank 使用独立原生
HTTP 适配层，因为 OpenAI API 没有统一 Rerank 协议。

Embedding 模型、URL 或维度变化后，停止服务并删除 `data/indexes/` 和
`data/app.db`，再依次重新调用项目、投标方和审核任务接口。

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

开发任务和验收顺序见 [tasks/README.md](tasks/README.md)。
