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
from app.parsers.registry import parser_registry
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
    payload = result.model_dump(exclude={"markdown_path"})
    checked = ParsedDocument.model_validate(payload)
    path = _parsed_path(checked.document.file_id)
    root = file_asset_root(get_settings().storage.data_root, checked.document.file_id)
    assets = assets_for(checked.document)
    for section in checked.sections:
        for block in section.blocks:
            for image in block.images:
                assets.read_image(image)  # 未登记/损坏图片不能进入已发布产物。
    # 版本目录不可变，JSON 入口最后原子切换。失败不覆盖旧图片/旧版本。
    markdown = render_document(checked)
    # 渲染规则变化也要生成新快照，不能用相同解析 JSON 的摘要覆盖旧 Markdown。
    version = hashlib.sha256(
        checked.model_dump_json().encode() + b"\0" + markdown.encode()
    ).hexdigest()
    version_dir = root / "versions" / version
    checked.markdown_path = f"{result.document.file_id}/versions/{version}/document.md"
    # 所有产物共用 parsed 根目录相对路径，版本目录不能重写模型可见的图片路径。
    atomic_write(version_dir / "document.md", markdown.encode())
    content = checked.model_dump_json(indent=2).encode()
    atomic_write(version_dir / "parsed.json", content)
    # 保留文件内最新副本；普通 Markdown 预览用根目录副本才能直接解析图片链接。
    # 程序仍按 JSON 的 markdown_path 读取同版本内容，以 parsed 目录为图片资源根。
    atomic_write(root / "document.md", markdown.encode())
    atomic_write(root.with_suffix(".md"), markdown.encode())
    atomic_write(path, content)
    (repository or FileRepository()).update_parse_status(
        checked.document.file_id, checked.parse_status
    )
    return checked


def load_parsed_document(file_id: str) -> ParsedDocument:
    return ParsedDocument.model_validate_json(
        _parsed_path(file_id).read_text(encoding="utf-8")
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
    """统一异步解析入口：原生解析在线程中执行，显式参数优先于视觉默认配置。"""
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
