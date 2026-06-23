"""投标文件详细清洗摘要、项目相关性路由及 JSON 持久化。"""

from pathlib import Path

from langchain_core.messages import SystemMessage

from app.core.config import AppSettings, ChatModelSettings
from app.llm.chat_model import invoke_structured
from app.models.documents import BidDocumentSummary, ParsedDocument
from app.models.rules import RuleSet
from app.prompts.loader import render_prompt


def build_bid_summary_context(
    parsed: ParsedDocument,
    *,
    max_chars: int = 48_000,
) -> str:
    """优先保留完整章节；超长时按章节顺序截断，避免破坏表格单元。"""

    blocks: list[str] = []
    for section in parsed.sections:
        body = "\n".join(
            part for part in [section.content, *section.tables] if part
        )
        if body:
            blocks.append(f"## {section.title}\n{body}")
    joined = "\n\n".join(blocks)
    if len(joined) <= max_chars:
        return joined

    selected: list[str] = []
    used = 0
    for block in blocks:
        if selected and used + len(block) > max_chars:
            break
        selected.append(block[: max_chars - used])
        used += len(selected[-1])
        if used >= max_chars:
            break
    return "\n\n".join(selected)


def rule_category_outline(rule_set: RuleSet) -> dict[str, list[str]]:
    outline: dict[str, list[str]] = {}
    for rule in rule_set.rules:
        outline.setdefault(str(rule.category), []).append(rule.name)
    return outline


async def summarize_and_route_bid_document(
    parsed: ParsedDocument,
    rule_set: RuleSet,
    model: object,
    model_settings: ChatModelSettings,
    *,
    confidence_threshold: float,
) -> BidDocumentSummary:
    prompt = render_prompt(
        "bid_document_summary",
        file_identity=parsed.document.model_dump(mode="json"),
        project_profile=rule_set.project_profile.model_dump(mode="json"),
        rule_category_outline=rule_category_outline(rule_set),
        document_context=build_bid_summary_context(parsed),
        bid_document_summary_schema=BidDocumentSummary.model_json_schema(),
    )
    result = await invoke_structured(
        model,
        [SystemMessage(content=prompt)],
        BidDocumentSummary,
        model_settings.structured_output_mode,
    )
    if result.file_id != parsed.document.file_id:
        result = result.model_copy(update={"file_id": parsed.document.file_id})
    if result.confidence < confidence_threshold:
        result = result.model_copy(
            update={
                "match_status": "uncertain",
                "match_reason": (
                    f"相关性判断置信度不足（{result.confidence:.2f}）："
                    f"{result.match_reason}"
                ),
            }
        )
    return result


def summary_path(file_id: str, settings: AppSettings) -> Path:
    return (
        settings.storage.data_root
        / "parsed"
        / "bid_summaries"
        / f"{file_id}.json"
    )


def write_bid_summary(
    summary: BidDocumentSummary,
    settings: AppSettings,
) -> Path:
    path = summary_path(summary.file_id, settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def load_bid_summary(
    file_id: str,
    settings: AppSettings,
) -> BidDocumentSummary:
    return BidDocumentSummary.model_validate_json(
        summary_path(file_id, settings).read_text("utf-8")
    )
