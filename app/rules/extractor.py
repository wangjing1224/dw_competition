"""从统一解析结果分批抽取规则，并绑定可信来源。"""

from typing import Any

from langchain_core.messages import SystemMessage
from pydantic import Field

from app.core.config import ChatModelSettings
from app.llm.chat_model import invoke_structured
from app.models.common import (
    EvidenceType,
    RuleCategory,
    RuleStatus,
    Severity,
    StrictModel,
    generate_id,
)
from app.models.documents import ParsedDocument, SourceRef
from app.models.rules import ProjectProfile, RuleItem, RuleScope, RuleSet
from app.prompts.loader import render_prompt


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
    rules: list[ExtractedRule] = Field(default_factory=list)


def _source_context(parsed: ParsedDocument) -> str:
    blocks: list[str] = []
    for section in parsed.sections:
        body = "\n".join(
            part for part in [section.content, *section.tables] if part
        )
        if body:
            blocks.append(f"[章节:{section.section_id}] {section.title}\n{body}")
    return "\n\n".join(blocks)


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
    """逐文件调用模型，汇总规则；大文件不会一次塞入整个项目上下文。"""

    rules: list[RuleItem] = []
    profile = ProjectProfile()
    for parsed in parsed_documents:
        context = _source_context(parsed)
        if not context:
            continue
        prompt = render_prompt(
            "rule_extraction",
            project_id=project_id,
            source_context=context,
            output_schema=RuleExtractionBatch.model_json_schema(),
        )
        batch = await invoke_structured(
            model,
            [SystemMessage(content=prompt)],
            RuleExtractionBatch,
            model_settings.structured_output_mode,
        )
        if not any(profile.model_dump(exclude_none=True).values()):
            profile = batch.project_profile
        source = _source_for_document(parsed)
        for extracted in batch.rules:
            rules.append(
                RuleItem(
                    rule_id=generate_id("rule"),
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
