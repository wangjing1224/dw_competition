"""从统一解析结果分批抽取规则，并绑定可信来源。"""

from typing import Any

from langchain_core.messages import SystemMessage
from pydantic import Field, model_validator

from app.core.config import ChatModelSettings
from app.llm.chat_model import invoke_structured
from app.llm.errors import InvalidStructuredOutput
from app.models.common import (
    EvidenceType,
    RuleCategory,
    RuleStatus,
    Severity,
    StrictModel,
)
from app.models.documents import ParsedDocument, SourceRef
from app.models.rules import ProjectProfile, RuleItem, RuleScope, RuleSet
from app.prompts.loader import render_prompt
from app.storage.ids import next_id


class ExtractedRule(StrictModel):
    """模型只输出规则语义字段；来源由程序绑定，禁止模型编造文件 ID。"""

    category: RuleCategory
    name: str
    requirement: str
    check_type: str
    target_field: str | None = None
    operator: str | None = None
    expected_value: Any | None = None
    scope: RuleScope = Field(default_factory=RuleScope)
    severity: Severity
    evidence_required: bool = False
    required_evidence_types: list[EvidenceType] = Field(default_factory=list)


class RuleExtractionBatch(StrictModel):
    project_profile: ProjectProfile = Field(default_factory=ProjectProfile)
    rules: list[ExtractedRule] = Field(default_factory=list, max_length=20)

    @model_validator(mode="before")
    @classmethod
    def normalize_flattened_project_profile(cls, value: Any) -> Any:
        """兼容模型将 ProjectProfile 字段误放在顶层的常见格式偏差。"""

        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        profile_fields = set(ProjectProfile.model_fields)
        flattened = {
            field: normalized.pop(field)
            for field in profile_fields
            if field in normalized
        }
        if not flattened:
            return normalized

        existing = normalized.get("project_profile")
        if existing is None:
            normalized["project_profile"] = flattened
        elif isinstance(existing, dict):
            # 正确嵌套字段优先，顶层字段只补充缺失值。
            normalized["project_profile"] = {**flattened, **existing}
        return normalized


def _source_blocks(parsed: ParsedDocument) -> list[str]:
    blocks: list[str] = []
    for section in parsed.sections:
        for unit_index, body in enumerate(
            part for part in [section.content, *section.tables] if part
        ):
            blocks.append(
                f"[章节:{section.section_id}][单元:{unit_index + 1}] "
                f"{section.title}\n{body}"
            )
    return blocks


def _split_oversized_block(
    block: str,
    *,
    max_chars: int,
    overlap_chars: int,
) -> list[str]:
    if len(block) <= max_chars:
        return [block]
    chunks: list[str] = []
    step = max_chars - overlap_chars
    for start in range(0, len(block), step):
        chunks.append(block[start : start + max_chars])
        if start + max_chars >= len(block):
            break
    return chunks


def build_rule_extraction_batches(
    parsed: ParsedDocument,
    *,
    max_chars: int = 12_000,
    overlap_chars: int = 600,
) -> list[str]:
    """按章节单元组批，避免小模型一次处理整份长文件。"""

    if overlap_chars >= max_chars:
        raise ValueError("overlap_chars 必须小于 max_chars")
    units = [
        chunk
        for block in _source_blocks(parsed)
        for chunk in _split_oversized_block(
            block,
            max_chars=max_chars,
            overlap_chars=overlap_chars,
        )
    ]
    batches: list[str] = []
    current: list[str] = []
    current_chars = 0
    for unit in units:
        additional = len(unit) + (2 if current else 0)
        if current and current_chars + additional > max_chars:
            batches.append("\n\n".join(current))
            current = []
            current_chars = 0
        current.append(unit)
        current_chars += len(unit) + (2 if len(current) > 1 else 0)
    if current:
        batches.append("\n\n".join(current))
    return batches


def _source_for_document(parsed: ParsedDocument) -> SourceRef:
    first = parsed.sections[0] if parsed.sections else None
    return SourceRef(
        file_id=parsed.document.file_id,
        file_name=parsed.document.file_name,
        source_path=parsed.document.source_path,
        section=first.title if first else None,
        quote=(
            first.content[:500]
            if first and first.content
            else "规则来源文件已解析，具体章节需通过原文回查工具定位"
        ),
    )


async def extract_rule_set(
    project_id: str,
    parsed_documents: list[ParsedDocument],
    model: object,
    model_settings: ChatModelSettings,
) -> RuleSet:
    """按文件和内容批次调用模型，汇总规则并定位失败批次。"""

    rules: list[RuleItem] = []
    profile = ProjectProfile()
    for parsed in parsed_documents:
        contexts = build_rule_extraction_batches(parsed)
        if not contexts:
            continue
        source = _source_for_document(parsed)
        for batch_index, context in enumerate(contexts, start=1):
            prompt = render_prompt(
                "rule_extraction",
                project_id=project_id,
                source_file=parsed.document.source_path,
                batch_index=batch_index,
                batch_count=len(contexts),
                max_rules=20,
                source_context=context,
                rule_extraction_schema=RuleExtractionBatch.model_json_schema(),
            )
            try:
                batch = await invoke_structured(
                    model,
                    [SystemMessage(content=prompt)],
                    RuleExtractionBatch,
                    model_settings.structured_output_mode,
                )
            except InvalidStructuredOutput as exc:
                raise InvalidStructuredOutput(
                    "规则抽取失败："
                    f"file={parsed.document.source_path}，"
                    f"batch={batch_index}/{len(contexts)}；{exc}"
                ) from exc
            if not any(profile.model_dump(exclude_none=True).values()):
                profile = batch.project_profile
            for extracted in batch.rules:
                rules.append(
                    RuleItem(
                        rule_id=next_id("rule"),
                        status=RuleStatus.ACTIVE,
                        source=source,
                        supporting_sources=[],
                        **extracted.model_dump(),
                    )
                )
    return RuleSet(
        project_id=project_id,
        project_profile=profile,
        rules=rules,
        source_files=[item.document for item in parsed_documents],
    )
