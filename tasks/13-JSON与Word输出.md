# 任务 13：JSON 与 Word 输出

## 1. 目标

在 LangGraph 返回后校验 AuditResult、原子写入任务级 JSON，并根据已写入 JSON 为每个适用文件生成固定模板 Word 报告。

## 2. 建议文件

```text
app/reports/
├── service.py
├── json_report.py
├── word_report.py
├── styles.py
└── templates/
```

### 2.1 文件职责与伪代码归属

| 文件 | 职责 | 对应伪代码 |
|---|---|---|
| `app/reports/service.py` | 输出总流程、任务状态更新和 JSON/Word 错误边界 | 4.1 |
| `app/reports/json_report.py` | AuditResult 校验和任务级 JSON 原子写入 | 4.2 |
| `app/reports/word_report.py` | 从已落盘 JSON 生成逐文件 Word、章节和来源索引 | 4.3、4.5 |
| `app/reports/styles.py` | Finding 分类/风险固定排序和 Word 样式 | 4.4 |
| `app/reports/templates/` | 可选 docx 模板和静态资源，不放业务判断 | 第 5 节报告章节 |
| `tests/unit/reports/test_json_report.py` | JSON 校验、原子写入和失败测试 | 第 6 节 |
| `tests/unit/reports/test_word_report.py` | 字段映射、排序、来源和不适用文件测试 | 第 6 节 |
| `tests/integration/test_report_service.py` | completed 与 report_error 状态测试 | 第 6 节 |

## 3. 核心约束

- JSON 是唯一事实来源。
- Word 生成前必须从已落盘 JSON 重新读取并校验。
- Word 不读取原始投标文件，不调用 LLM，不修改审核结论。
- 不适用文件不生成 Word。
- Word 失败不删除 JSON，也不重跑 Agent。

## 4. 核心伪代码

### 4.1 输出总流程

```python
async def finalize_outputs(task_id, raw_audit_result):
    try:
        audit_result = validate_audit_result(raw_audit_result)
        json_path = write_json_result(task_id, audit_result)
    except Exception as exc:
        # failed 状态统一由后台 Runner 收口，避免重复状态转换。
        raise AuditResultOutputError(str(exc)) from exc

    try:
        word_paths = generate_word_reports(json_path)
        task_repository.mark_completed(
            task_id,
            json_path=json_path,
            word_paths=word_paths,
        )
    except Exception as exc:
        task_repository.mark_completed_with_report_error(
            task_id,
            json_path=json_path,
            word_paths=list_existing_word_outputs(task_id),
            error_message=str(exc),
        )
```

独立重试只重新渲染 Word：

```python
def rerender_word_reports(task_id):
    task = task_repository.require(task_id)
    audit_result = read_and_validate_json(task.json_path)
    word_paths = generate_word_reports(task.json_path)
    task_repository.mark_completed(
        task_id,
        json_path=task.json_path,
        word_paths=word_paths,
    )
    return word_paths
```

### 4.2 JSON 原子写入

```python
def write_json_result(task_id, audit_result):
    output_dir = file_store.task_output_dir(task_id)
    final_path = output_dir / f"{task_id}_审核结果.json"
    temp_path = final_path.with_suffix(".json.tmp")

    temp_path.write_text(
        audit_result.model_dump_json(
            indent=2,
            exclude_none=False,
        ),
        encoding="utf-8",
    )

    # 写入后再次读取，避免编码或截断问题。
    AuditResult.model_validate_json(temp_path.read_text("utf-8"))
    atomic_replace(temp_path, final_path)
    return relative_to_data_root(final_path)
```

### 4.3 Word 生成入口

```python
def generate_word_reports(json_path):
    audit_result = AuditResult.model_validate_json(
        file_store.resolve(json_path).read_text("utf-8")
    )
    paths = []

    for file_result in audit_result.file_results:
        if not file_result.is_applicable:
            continue

        document = create_report_document()
        render_overview(document, audit_result, file_result)
        render_project_match(document, file_result.project_match)
        render_findings_by_category(document, file_result.findings)
        render_evidence_reviews(document, file_result.evidence_results)
        render_risks(document, file_result.findings)
        render_manual_reviews(document, file_result.findings)
        render_suggestions(document, file_result.suggestions)
        render_source_index(document, file_result)

        path = save_word_atomic(
            document,
            task_id=audit_result.task_id,
            bid_file=file_result.file,
        )
        paths.append(path)

    return paths
```

### 4.4 固定排序

```python
SEVERITY_ORDER = {
    "fatal": 0,
    "major": 1,
    "minor": 2,
    "unknown": 3,
    "info": 4,
}

def ordered_findings(findings):
    return sorted(
        findings,
        key=lambda item: (
            CATEGORY_ORDER[item.category],
            SEVERITY_ORDER[item.severity],
            item.rule_id,
        ),
    )
```

### 4.5 来源索引

```python
def collect_source_index(file_result):
    sources = []
    for finding in file_result.findings:
        sources.append(("规则依据", finding.rule_source))
        if finding.bid_source:
            sources.append(("投标响应", finding.bid_source))

    for review in file_result.evidence_results:
        for source in review.evidence_found:
            sources.append(("支撑材料", source))

    return deduplicate_by(
        sources,
        key=lambda item: (item[1].file_id, item[1].chunk_id),
    )
```

## 5. 报告章节

固定包含：

1. 审核概览
2. 项目适用性判断
3. 形式与商务审核
4. 技术响应审核
5. 资格与资料核验
6. 跨文档一致性检查
7. 不合规项和风险项
8. 无法判定与人工复核项
9. 整改建议
10. 依据与证据索引

## 6. 测试设计

- JSON Schema 校验失败时不生成任何 Word。
- JSON 写入成功、Word 失败时状态为 `completed_with_report_error`。
- 不适用文件不生成 Word，但保留在任务级 JSON。
- Word 中的结论数量和 JSON Finding 数量一致。
- 来源索引能回到规则、投标响应和证据文件。
- 中文文件名安全处理，系统文件名仍避免冲突。
- 重新渲染 Word 不改变 JSON。
- `completed_with_report_error` 可只调用 `rerender_word_reports` 恢复，不重跑 Agent。

## 7. 验收标准

- 一个任务只生成一份任务级 JSON。
- 每个适用投标文件生成一份 Word。
- 报告内容完全来自 JSON。
- 输出路径保存为相对路径。
- Word 渲染错误可独立重试。
