"""构造单文件摘要并调用模型完成投标方资料分类。"""

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.config import ChatModelSettings
from app.llm.chat_model import invoke_structured
from app.models.common import EvidenceType
from app.models.documents import DocumentRef, ParsedDocument
from app.models.evidence import EvidenceClassification
from app.prompts.loader import render_prompt


def build_classification_input(
    document: DocumentRef,
    parsed: ParsedDocument,
) -> dict[str, object]:
    """只发送分类需要的文件身份、标题和首尾正文。"""

    texts = [
        part
        for section in parsed.sections
        for part in [section.content, *section.tables]
        if part
    ]
    joined = "\n".join(texts)
    return {
        "file_name": document.file_name,
        "relative_directory": document.source_path.rpartition("/")[0],
        "titles": [section.title for section in parsed.sections[:20]],
        "content_head": joined[:4000],
        "content_tail": joined[-2000:] if len(joined) > 4000 else "",
        "parse_status": str(parsed.parse_status),
        "image_count": sum(section.image_count for section in parsed.sections),
        "warnings": parsed.warnings[:10],
    }


async def classify_evidence_document(
    document: DocumentRef,
    parsed: ParsedDocument,
    model: object,
    model_settings: ChatModelSettings,
    **_: object,
) -> EvidenceClassification:
    """图片资料仍允许识别类型，但确定性降级为人工复核。"""

    result = await invoke_structured(
        model,
        [
            SystemMessage(
                content=render_prompt(
                    "evidence_classification",
                    evidence_types=[item.value for item in EvidenceType],
                    evidence_classification_schema=(
                        EvidenceClassification.model_json_schema()
                    ),
                )
            ),
            HumanMessage(
                content=render_prompt(
                    "evidence_classification_input",
                    classification_input=build_classification_input(
                        document,
                        parsed,
                    ),
                )
            ),
        ],
        EvidenceClassification,
        model_settings.structured_output_mode,
    )
    if str(parsed.parse_status) in {
        "unsupported",
        "ocr_required",
        "manual_review",
    }:
        return result.model_copy(
            update={
                "classification_status": "manual_review",
                "reason": (
                    f"{result.reason}；当前文件正文无法可靠读取，需要人工核验"
                ),
                "bid_summary": (
                    result.bid_summary.rstrip("。")
                    + "，正文无法可靠读取，需人工核验"
                ),
            }
        )
    return result
