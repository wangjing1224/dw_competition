"""统一选择解析器、持久化解析 JSON 并更新文件状态。"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from app.core.config import get_settings
from app.models.documents import DocumentRef, ParsedDocument
from app.parsers.assets import atomic_write, file_asset_root
from app.parsers.base import RecoverableParseError
from app.parsers.common import assets_for
from app.parsers.registry import ParserRegistry, parser_registry
from app.parsers.render import render_document
from app.storage.file_store import FileStore
from app.storage.repositories import FileRepository


def _parsed_path(file_id: str) -> Path:
    root = file_asset_root(get_settings().storage.data_root, file_id)
    return root.with_suffix(".json")


def persist_parse_result(
    result: ParsedDocument,
    repository: FileRepository | None = None,
) -> ParsedDocument:
    path = _parsed_path(result.document.file_id)
    root = file_asset_root(get_settings().storage.data_root, result.document.file_id)
    assets = assets_for(result.document)
    for section in result.sections:
        for block in section.blocks:
            for image in block.images:
                assets.read_image(image)  # 未登记/损坏图片不能进入已发布产物。
    # 版本目录不可变，JSON 入口最后原子切换。失败不覆盖旧图片/旧版本。
    payload = result.model_dump(exclude={"markdown_path"})
    checked = ParsedDocument.model_validate(payload)
    version = hashlib.sha256(checked.model_dump_json().encode()).hexdigest()
    markdown = render_document(checked)
    version_dir = root / "versions" / version
    checked.markdown_path = f"{result.document.file_id}/versions/{version}/document.md"
    linked_markdown = markdown.replace("](images/", "](../../images/").replace(
        'src="images/', 'src="../../images/'
    )
    atomic_write(version_dir / "document.md", linked_markdown.encode())
    content = checked.model_dump_json(indent=2).encode()
    atomic_write(version_dir / "parsed.json", content)
    # 便于人工浏览的副本；程序按 JSON 的 markdown_path 读取同版本内容。
    atomic_write(root / "document.md", markdown.encode())
    atomic_write(path, content)
    (repository or FileRepository()).update_parse_status(
        checked.document.file_id, checked.parse_status
    )
    return checked


def load_parsed_document(file_id: str) -> ParsedDocument:
    return ParsedDocument.model_validate_json(
        _parsed_path(file_id).read_text(encoding="utf-8")
    )


def parse_document(
    document: DocumentRef,
    *,
    file_store: FileStore | None = None,
    registry: ParserRegistry | None = None,
    repository: FileRepository | None = None,
) -> ParsedDocument:
    """兼容现有同步入口：只做原生解析，不暗中发起付费视觉请求。"""
    return persist_parse_result(
        _parse_native(
            document, file_store=file_store, registry=registry, repository=repository
        ),
        repository,
    )


def _parse_native(document, *, file_store=None, registry=None, repository=None):
    store = file_store or FileStore(repository)
    selected_registry = registry or parser_registry
    stored_path = store.resolve(document.file_id)
    parser = selected_registry.get(stored_path.suffix.lower())
    if parser is None:
        return ParsedDocument(
            document=document.model_copy(update={"parse_status": "unsupported"}),
            parse_status="unsupported",
            warnings=["当前格式没有可用解析器"],
        )
    try:
        return parser.parse(document, stored_path)
    except RecoverableParseError as exc:
        partial = exc.partial_result.model_copy(
            update={
                "parse_status": "partial",
                "document": exc.partial_result.document.model_copy(
                    update={"parse_status": "partial"}
                ),
            }
        )
        return partial
    except Exception as exc:
        return ParsedDocument(
            document=document.model_copy(update={"parse_status": "manual_review"}),
            parse_status="manual_review",
            warnings=[f"解析失败: {type(exc).__name__}"],
        )


async def prepare_document(
    document: DocumentRef,
    *,
    file_store=None,
    registry=None,
    repository=None,
    vision_enabled: bool | None = None,
    vision_model=None,
) -> ParsedDocument:
    """Task19 公共异步入口；Task21/22 再接入业务循环，不在这里实现 Agent。"""
    result = await asyncio.to_thread(
        _parse_native,
        document,
        file_store=file_store,
        registry=registry,
        repository=repository,
    )
    if result.schema_version == 2:
        root = file_asset_root(get_settings().storage.data_root, document.file_id)
        native_digest = hashlib.sha256(result.model_dump_json().encode()).hexdigest()
        await asyncio.to_thread(
            atomic_write,
            root / "checkpoints" / f"{native_digest}.json",
            result.model_dump_json(indent=2).encode(),
        )
        enabled = (
            get_settings().parsing.vision_enabled
            if vision_enabled is None
            else vision_enabled
        )
        if enabled:
            from app.parsers.vision import enrich_document

            result = await enrich_document(
                result, assets_for(document), model=vision_model
            )
    return await asyncio.to_thread(persist_parse_result, result, repository)
