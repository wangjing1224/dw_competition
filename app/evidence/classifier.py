"""构造确定性摘要并调用模型完成投标方资料分类。"""

from langchain_core.messages import SystemMessage

from app.core.config import ChatModelSettings
from app.llm.chat_model import invoke_structured
from app.models.common import EvidenceType
from app.models.documents import DocumentRef, ParsedDocument
from app.models.evidence import EvidenceClassification
from app.prompts.loader import render_prompt


def build_classification_input(
    document: DocumentRef, parsed: ParsedDocument
) -> dict[str, object]:
    """只发送足够分类的标题、首尾文本和表头，避免上传整份长文档。"""

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
        "content_head": joined[:3000],
        "content_tail": joined[-1500:] if len(joined) > 3000 else "",
        "parse_status": parsed.parse_status,
    }


async def classify_evidence_document(
    document: DocumentRef,
    parsed: ParsedDocument,
    model: object,
    model_settings: ChatModelSettings,
    *,
    confidence_threshold: float = 0.65,
) -> EvidenceClassification:
    """解析不足直接人工复核；低置信度结果统一降级为 other。"""

    if parsed.parse_status in {"unsupported", "ocr_required", "manual_review"}:
        return EvidenceClassification(
            evidence_type="other",
            confidence=0,
            classification_status="manual_review",
            reason="当前解析结果不足以可靠自动分类",
        )
    prompt = render_prompt(
        "evidence_classification",
        classification_input=build_classification_input(document, parsed),
        evidence_types=[item.value for item in EvidenceType],
        output_schema=EvidenceClassification.model_json_schema(),
    )
    result = await invoke_structured(
        model,
        [SystemMessage(content=prompt)],
        EvidenceClassification,
        model_settings.structured_output_mode,
    )
    if result.confidence < confidence_threshold:
        return result.model_copy(
            update={
                "evidence_type": "other",
                "classification_status": "manual_review",
            }
        )
    return result
