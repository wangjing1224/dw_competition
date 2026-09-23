"""投标文件详细清洗摘要、项目相关性路由及 JSON 持久化。"""

from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.config import AppSettings, ChatModelSettings
from app.llm.chat_model import invoke_structured
from app.models.documents import BidDocumentSummary, BidRouteDecision, ParsedDocument
from app.models.rules import RuleSet
from app.prompts.loader import render_prompt

RULE_CATEGORY_GUIDE = {
    "format": "签字盖章、文件组成、授权和格式完整性",
    "qualification": "主体资格、证照、资质和准入条件",
    "commercial": "报价、税率、有效期、交付和商务响应",
    "technical": "产品、型号、参数、性能、接口和标准响应",
    "performance_capacity": "合同业绩、生产能力、人员和售后能力",
    "consistency_risk": "跨章节、附件和资料的主体、型号、数量等一致性",
}


def build_bid_summary_context(
    parsed: ParsedDocument,
    *,
    max_chars: int = 180_000,
) -> str:
    """优先保留完整章节；极端超长文件才按章节边界截断。"""

    blocks = [
        f"## {section.title}\n"
        + "\n".join(part for part in [section.content, *section.tables] if part)
        for section in parsed.sections
        if section.content or section.tables
    ]
    joined = "\n\n".join(blocks)
    if len(joined) <= max_chars:
        return joined
    selected: list[str] = []
    used = 0
    for block in blocks:
        remaining = max_chars - used
        if remaining <= 0:
            break
        selected.append(block[:remaining])
        used += len(selected[-1])
    return "\n\n".join(selected)


async def summarize_and_route_bid_document(
    parsed: ParsedDocument,
    rule_set: RuleSet,
    model: object,
    model_settings: ChatModelSettings,
    **_: object,
) -> BidDocumentSummary:
    result = await invoke_structured(
        model,
        [
            SystemMessage(
                content=render_prompt(
                    "bid_document_summary",
                    bid_document_summary_schema=(BidRouteDecision.model_json_schema()),
                )
            ),
            HumanMessage(
                content=render_prompt(
                    "bid_document_summary_input",
                    file_identity={
                        "file_name": parsed.document.file_name,
                        "source_path": parsed.document.source_path,
                    },
                    project_profile=rule_set.project_profile,
                    rule_categories=RULE_CATEGORY_GUIDE,
                    document_context=build_bid_summary_context(parsed),
                )
            ),
        ],
        BidRouteDecision,
        model_settings.structured_output_mode,
    )
    return BidDocumentSummary(
        file_id=parsed.document.file_id,
        **result.model_dump(),
    )


def summary_path(file_id: str, settings: AppSettings) -> Path:
    return settings.storage.data_root / "parsed" / "bid_summaries" / f"{file_id}.json"


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
