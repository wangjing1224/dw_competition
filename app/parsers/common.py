"""解析器共用状态计算；不决定资料分类或审核结论。"""

import hashlib
from pathlib import Path

from app.core.config import get_settings
from app.models.documents import DocumentRef, ParsedDocument, ParsedSection
from app.parsers.assets import AssetStore, file_asset_root

PARSER_VERSION = "2.3.0"


def assets_for(document: DocumentRef) -> AssetStore:
    settings = get_settings()
    return AssetStore(
        file_asset_root(settings.storage.data_root, document.file_id),
        max_bytes=settings.parsing.max_image_bytes,
        max_pixels=settings.parsing.max_image_pixels,
    )


def finish_document(
    document: DocumentRef,
    path: Path,
    sections: list[ParsedSection],
    warnings: list[str] | None = None,
) -> ParsedDocument:
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    result = ParsedDocument(
        schema_version=2,
        parser_version=PARSER_VERSION,
        source_sha256=digest,
        document=document,
        parse_status="pending",
        sections=sections,
        warnings=warnings or [],
    )
    return refresh_status(result)


def refresh_status(result: ParsedDocument) -> ParsedDocument:
    # 子对象更新后重新投影兼容字段，不能依赖 list.append 触发模型校验。
    result.sections = [
        ParsedSection.model_validate(s.model_dump()) for s in result.sections
    ]
    if result.schema_version == 2:
        for section in result.sections:
            section.project_blocks()
    blocks = [b for s in result.sections for b in s.blocks]
    images = [i for b in blocks for i in b.images if i.purpose == "content"]
    has_text = any(b.content.strip() for b in blocks) or any(
        i.transcription.strip() for i in images
    )
    unread = any(i.read_status not in {"readable", "skipped"} for i in images)
    incomplete = unread or bool(result.warnings) or any(b.warnings for b in blocks)
    if has_text:
        status = "partial" if incomplete else "parsed"
    elif images:
        status = "ocr_required"
    else:
        status = "manual_review"
    result.parse_status = status
    result.document = result.document.model_copy(update={"parse_status": status})
    return result
