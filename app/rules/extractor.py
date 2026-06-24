"""从招标文件串行抽取规则增量，并维护单一完整 RuleSet。"""

import inspect
from collections.abc import Awaitable, Callable

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.config import ChatModelSettings
from app.llm.chat_model import invoke_structured
from app.llm.errors import InvalidStructuredOutput
from app.models.documents import ParsedDocument, SourceRef
from app.models.rules import (
    ProjectProfile,
    RuleDeltaResult,
    RuleExtractionProgress,
    RuleSet,
    present_rules_for_model,
)
from app.prompts.loader import render_prompt
from app.rules.merger import (
    apply_rule_delta_operations,
    compact_rule_set,
    finalize_rule_set,
)


RuleProgressCallback = Callable[
    [RuleExtractionProgress],
    Awaitable[None] | None,
]


def _source_blocks(parsed: ParsedDocument) -> list[str]:
    blocks: list[str] = []
    for section in parsed.sections:
        for unit_index, body in enumerate(
            part for part in [section.content, *section.tables] if part
        ):
            blocks.append(
                f"[章节:{section.section_id}][单元:{unit_index + 1}]\n"
                f"## {section.title}\n{body}"
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
    """按章节和表格组批；超长单元才使用带重叠的字符切分。"""

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
        current_chars += additional
    if current:
        batches.append("\n\n".join(current))
    return batches


def _source_for_batch(parsed: ParsedDocument, context: str) -> SourceRef:
    first_line = next(
        (line.removeprefix("## ").strip() for line in context.splitlines() if line.startswith("## ")),
        None,
    )
    return SourceRef(
        file_id=parsed.document.file_id,
        file_name=parsed.document.file_name,
        source_path=parsed.document.source_path,
        section=first_line,
        quote=context[:500],
    )


def _merge_profile(current: ProjectProfile, incoming: ProjectProfile) -> ProjectProfile:
    values = current.model_dump()
    for key, value in incoming.model_dump().items():
        if values.get(key) in {None, ""} and value not in {None, ""}:
            values[key] = value
    return ProjectProfile.model_validate(values)


async def _invoke_delta(
    *,
    model: object,
    settings: ChatModelSettings,
    rule_set: RuleSet,
    parsed: ParsedDocument,
    context: str,
    batch_index: int,
    batch_count: int,
) -> RuleDeltaResult:
    messages = [
        SystemMessage(
            content=render_prompt(
                "rule_extraction",
                rule_delta_schema=RuleDeltaResult.model_json_schema(),
            )
        ),
        HumanMessage(
            content=render_prompt(
                "rule_extraction_input",
                current_rules=[
                    item.model_dump(mode="json")
                    for item in present_rules_for_model(rule_set)
                ],
                source_file=parsed.document.source_path,
                batch_index=batch_index,
                batch_count=batch_count,
                source_context=context,
            )
        ),
    ]
    return await invoke_structured(
        model,
        messages,
        RuleDeltaResult,
        settings.structured_output_mode,
    )


async def _report_progress(
    callback: RuleProgressCallback | None,
    progress: RuleExtractionProgress,
) -> None:
    if callback is None:
        return
    result = callback(progress)
    if inspect.isawaitable(result):
        await result


async def extract_rule_set(
    project_id: str,
    parsed_documents: list[ParsedDocument],
    model: object,
    model_settings: ChatModelSettings,
    *,
    maximum: int = 30,
    progress_callback: RuleProgressCallback | None = None,
) -> RuleSet:
    """严格串行执行每个文件批次，使模型始终看到最新规则集合。"""

    rule_set = RuleSet(
        project_id=project_id,
        source_files=[item.document for item in parsed_documents],
    )
    for document_index, parsed in enumerate(parsed_documents, start=1):
        contexts = build_rule_extraction_batches(parsed)
        for batch_index, context in enumerate(contexts, start=1):
            try:
                delta = await _invoke_delta(
                    model=model,
                    settings=model_settings,
                    rule_set=rule_set,
                    parsed=parsed,
                    context=context,
                    batch_index=batch_index,
                    batch_count=len(contexts),
                )
            except InvalidStructuredOutput as exc:
                raise InvalidStructuredOutput(
                    "规则抽取失败："
                    f"file={parsed.document.source_path}，"
                    f"batch={batch_index}/{len(contexts)}；{exc}"
                ) from exc
            rule_set = rule_set.model_copy(
                update={
                    "project_profile": _merge_profile(
                        rule_set.project_profile,
                        delta.project_profile,
                    )
                }
            )
            rule_set = apply_rule_delta_operations(
                rule_set,
                delta.operations,
                source=_source_for_batch(parsed, context),
            )
            if len(rule_set.rules) > maximum:
                rule_set = compact_rule_set(
                    rule_set,
                    maximum=maximum,
                )
            await _report_progress(
                progress_callback,
                RuleExtractionProgress(
                    project_id=project_id,
                    document_index=document_index,
                    document_count=len(parsed_documents),
                    batch_index=batch_index,
                    batch_count=len(contexts),
                    source_file=parsed.document.source_path,
                    rule_count=len(rule_set.rules),
                    rule_set=rule_set,
                ),
            )
    return finalize_rule_set(rule_set, maximum=maximum)
