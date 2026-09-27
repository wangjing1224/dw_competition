"""协调解析结果分段、Embedding 和本地索引原子构建。"""

import hashlib
import json

import numpy as np

from app.core.config import AppSettings, EmbeddingSettings, get_settings
from app.llm.embedding_model import EmbeddingBatchError, EmbeddingModel
from app.llm.errors import ModelCallError
from app.models.documents import DocumentRef
from app.parsers.service import load_parsed_document
from app.retrieval.chunker import chunk_parsed_document
from app.retrieval.keyword import build_keyword_index
from app.retrieval.models import IndexManifest, IndexScope
from app.retrieval.store import atomic_index_directory, write_index


def embedding_config_fingerprint(settings: EmbeddingSettings) -> str:
    # Key 不影响向量语义，故意不进入指纹。
    payload = {
        "platform": settings.platform,
        "base_url": settings.base_url.rstrip("/"),
        "model": settings.model,
        "dimension": settings.dimension,
        "text_modes": "document/query"
        if settings.platform == "bailian"
        else "openai-compatible",
        "input_version": "section-hint-content-v1",
    }
    encoded = json.dumps(payload, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


async def build_document_index(
    scope: IndexScope,
    documents: list[DocumentRef],
    embedding_model: EmbeddingModel,
    settings: AppSettings | None = None,
) -> IndexManifest:
    resolved = settings or get_settings()
    chunks = []
    for document in documents:
        if (
            document.data_role != scope.data_role
            or document.project_id != scope.project_id
            or document.bidder_id != scope.bidder_id
            or document.task_id != scope.task_id
        ):
            raise ValueError("文档角色与索引范围不一致")
        parsed = load_parsed_document(document.file_id)
        for key in ("file_id", "project_id", "bidder_id", "task_id", "data_role"):
            if getattr(parsed.document, key) != getattr(document, key):
                raise ValueError("解析文件身份与上传记录不一致")
        # 入库 Agent 启动前只索引原文，不读取旧分类或把文件摘要重复混入每个切片。
        chunks.extend(
            chunk_parsed_document(
                parsed,
                max_tokens=resolved.retrieval.chunk_tokens,
                overlap_tokens=resolved.retrieval.chunk_overlap,
            )
        )
    try:
        vectors = (
            await embedding_model.embed(
                [
                    "\n".join(
                        part
                        for part in [
                            chunk.section,
                            chunk.retrieval_hint,
                            chunk.content,
                        ]
                        if part
                    )
                    for chunk in chunks
                ]
            )
            if chunks
            else np.empty((0, resolved.embedding_model.dimension), dtype=np.float32)
        )
    except ModelCallError as exc:
        failed = (
            chunks[exc.start : exc.end]
            if isinstance(exc, EmbeddingBatchError)
            else chunks
        )
        locations = ",".join(f"{chunk.file_id}/{chunk.chunk_id}" for chunk in failed)
        raise ModelCallError(
            exc.code,
            f"{exc.message}；索引 {scope.scope_id}；失败批次文件/片段：{locations}",
            retryable=exc.retryable,
        ) from exc
    manifest = IndexManifest(
        dimension=resolved.embedding_model.dimension,
        chunk_count=len(chunks),
        embedding_model=resolved.embedding_model.model,
        embedding_fingerprint=embedding_config_fingerprint(resolved.embedding_model),
    )
    with atomic_index_directory(scope.scope_id) as directory:
        write_index(
            directory,
            manifest,
            chunks,
            vectors,
            build_keyword_index(chunks),
        )
    return manifest
