# 任务 02：核心领域模型与 Schema

## 1. 目标

把设计文档中的共享枚举、运行时契约和最终输出模型固化为 Pydantic 模型，并生成稳定 JSON Schema。后续所有模块只能复用这些模型，不能各自定义近义字段。

## 2. 建议文件

```text
app/models/
├── common.py
├── documents.py
├── rules.py
├── evidence.py
├── findings.py
├── tasks.py
├── results.py
└── schema_export.py
```

### 2.1 文件职责与伪代码归属

| 文件 | 职责 | 对应伪代码/模型 |
|---|---|---|
| `app/models/common.py` | `StrictModel`、共享枚举、`ComparisonResult` 和通用校验辅助函数 | 5.1 |
| `app/models/documents.py` | `DocumentRef`、`SourceRef`、`RetrievedSource`、解析结果模型 | 5.2 |
| `app/models/rules.py` | `RuleScope`、`RuleItem`、`RuleSet` | 5.3 |
| `app/models/evidence.py` | `EvidenceClassification`、`Claim`、`EvidenceCandidate`、`EvidenceReviewResult` | 第 3 节证据模型 |
| `app/models/findings.py` | `Finding` 及其来源、结果、风险字段 | 第 3 节 Finding |
| `app/models/tasks.py` | `TaskError`、Decision 模型和 `BidAgentDecision` 判别联合 | 5.4 |
| `app/models/results.py` | `ProjectMatch`、`FileAuditResult`、`AuditResult` 及跨对象归属校验 | 5.5 |
| `app/models/schema_export.py` | 将指定模型导出到 `schemas/*.schema.json` | 第 6 节导出伪代码 |
| `tests/unit/models/` | 为模型和交叉校验编写单元测试 | 第 7 节 |

## 3. 模型范围

### 3.1 枚举

- `DataRole`
- `ParseStatus`
- `EvidenceType`
- `ClassificationStatus`
- `RuleStatus`
- `RuleCategory`
- `Severity`
- `ResponseStatus`
- `FindingResult`
- `EvidenceReviewStatus`
- `ProjectMatchStatus`
- `ProjectStatus`
- `BidderStatus`
- `OverallConclusion`
- `TaskStatus`

### 3.2 核心模型

- `DocumentRef`
- `SourceRef`
- `RetrievedSource`
- `ParsedDocument`、`ParsedSection`
- `RuleSet`、`RuleItem`
- `Claim`
- `EvidenceCandidate`
- `EvidenceClassification`
- `EvidenceReviewResult`
- `ProjectMatch`
- `ComparisonResult`
- `Finding`
- `FileAuditResult`
- `AuditResult`
- `TaskError`
- `BidAgentDecision`

## 4. 核心约束

- 所有 ID 使用非空字符串；生成策略由普通函数负责。
- 最终结果模型默认拒绝额外字段，防止 LLM 偷渡未定义内容。
- `SourceRef` 不能只有文件名，必须包含 `file_id` 和 `source_path`。
- `RuleItem.status != active` 时不能作为普通自动审核规则执行。
- `Finding.response_status == not_found` 时允许 `bid_source = None`。
- `record_project_match`、`record_finding` 与 `record_file_result` 使用判别联合保证字段互斥。
- 证据类型只使用统一 `EvidenceType`。

## 5. 核心伪代码

### 5.1 基础模型配置

```python
class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",          # LLM 或 API 不得携带未知字段
        validate_assignment=True,
        use_enum_values=True,
    )
```

### 5.2 来源引用

```python
class SourceRef(StrictModel):
    file_id: str
    chunk_id: str | None = None
    file_name: str
    source_path: str
    section: str | None = None
    quote: str | None = None

    @model_validator(mode="after")
    def validate_traceability(self):
        # 最终结论至少能回查到具体文件。
        if not self.file_id or not self.source_path:
            raise ValueError("source reference is not traceable")
        return self
```

### 5.3 RuleItem

```python
class RuleItem(StrictModel):
    rule_id: str
    status: RuleStatus
    category: RuleCategory
    name: str
    requirement: str
    check_type: str
    target_field: str | None
    operator: str | None
    expected_value: Any | None
    scope: RuleScope
    severity: Severity
    evidence_required: bool
    required_evidence_types: list[EvidenceType]
    conflict_note: str | None
    source: SourceRef
    supporting_sources: list[SourceRef]

    @model_validator(mode="after")
    def validate_evidence_types(self):
        if self.evidence_required and not self.required_evidence_types:
            raise ValueError("evidence-required rule needs evidence types")
        return self
```

### 5.4 BidAgentDecision

```python
class RecordFindingDecision(StrictModel):
    action: Literal["record_finding"]
    reason: str
    finding: Finding
    project_match: None = None
    is_applicable: None = None
    exclusion_reason: None = None
    file_result: None = None
    evidence_results: list[EvidenceReviewResult] = Field(default_factory=list)


class RecordProjectMatchDecision(StrictModel):
    action: Literal["record_project_match"]
    reason: str
    project_match: ProjectMatch
    is_applicable: bool
    exclusion_reason: str | None = None
    finding: None = None
    file_result: None = None
    evidence_results: list[EvidenceReviewResult] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_applicability(self):
        # uncertain 继续审核并在结果中保留人工复核提示；not_matched 才排除。
        if self.project_match.status == "not_matched":
            if self.is_applicable or not self.exclusion_reason:
                raise ValueError("not_matched file must be excluded with reason")
        elif not self.is_applicable:
            raise ValueError("matched or uncertain file continues auditing")
        return self


class RecordFileResultDecision(StrictModel):
    action: Literal["record_file_result"]
    reason: str
    project_match: None = None
    is_applicable: None = None
    exclusion_reason: None = None
    finding: None = None
    file_result: FileAuditResult
    evidence_results: list[EvidenceReviewResult] = Field(default_factory=list)


BidAgentDecision = Annotated[
    RecordProjectMatchDecision | RecordFindingDecision | RecordFileResultDecision,
    Field(discriminator="action"),
]

BidAgentDecisionAdapter = TypeAdapter(BidAgentDecision)
```

### 5.5 最终结果交叉校验

```python
class AuditResult(StrictModel):
    schema_version: Literal["1.0"]
    task_id: str
    project_id: str
    bidder_id: str
    file_results: list[FileAuditResult]

    @model_validator(mode="after")
    def validate_identity_and_coverage(self):
        file_ids = set()
        for file_result in self.file_results:
            if file_result.file.file_id in file_ids:
                raise ValueError("duplicate file result")
            file_ids.add(file_result.file.file_id)

            # 每个内部证据结果和 Finding 都必须属于当前根对象。
            assert_all_ids_match(
                file_result,
                task_id=self.task_id,
                project_id=self.project_id,
                bidder_id=self.bidder_id,
            )
        return self
```

## 6. Schema 产物

建议在测试或构建脚本中导出：

```text
schemas/rule_set.schema.json
schemas/evidence_classification.schema.json
schemas/bid_agent_decision.schema.json
schemas/evidence_review_result.schema.json
schemas/audit_result.schema.json
```

导出伪代码：

```python
for schema_type, output_path in schema_targets:
    # 判别联合不是 BaseModel 子类，统一通过 TypeAdapter 导出。
    schema = TypeAdapter(schema_type).json_schema()
    write_json_atomic(output_path, schema)
```

## 7. 测试设计

- 所有设计文档示例 JSON 均可通过对应模型校验。
- 未知枚举值、额外字段、空 ID、错误 ID 归属应校验失败。
- `record_finding` 缺少 Finding 时失败。
- `record_project_match` 缺少 ProjectMatch 时失败。
- `not_found` Finding 可无 `bid_source`，`found` Finding 必须有 `bid_source`。
- 不适用文件不得包含普通审核 Findings。
- `AuditResult` 内重复文件结果校验失败。

## 8. 验收标准

- 后续模块需要的共享模型全部存在。
- Schema 可稳定导出且纳入测试快照。
- 模型可表达设计文档中的完整示例，不依赖自由字典补字段。
- 枚举名称在 Prompt、索引和输出中可直接复用。
