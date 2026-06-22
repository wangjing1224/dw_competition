"""协调解析结果分段、Embedding 和本地索引原子构建。"""

import hashlib
import json

from app.core.config import AppSettings, EmbeddingSettings, get_settings
from app.llm.embedding_model import EmbeddingModel
from app.models.documents import DocumentRef
from app.parsers.service import load_parsed_document
from app.retrieval.chunker import chunk_parsed_document
from app.retrieval.keyword import build_keyword_index
from app.retrieval.models import IndexManifest, IndexScope
from app.retrieval.store import atomic_index_directory, write_index


def embedding_config_fingerprint(settings: EmbeddingSettings) -> str:
    # Key 不影响向量语义，故意不进入指纹。
    payload = {
        "base_url": settings.base_url.rstrip("/"),
        "model": settings.model,
        "dimension": settings.dimension,
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
        if document.data_role != scope.data_role:
            raise ValueError("文档角色与索引范围不一致")
        parsed = load_parsed_document(document.file_id)
        # 解析 JSON 可能早于资料分类完成；索引必须采用调用方传入的最新
        # evidence_type/classification_status，而不是旧解析快照中的元数据。
        parsed = parsed.model_copy(update={"document": document})
        chunks.extend(
            chunk_parsed_document(
                parsed,
                max_tokens=resolved.retrieval.chunk_tokens,
                overlap_tokens=resolved.retrieval.chunk_overlap,
            )
        )
    vectors = await embedding_model.embed(
        [f"{chunk.section}\n{chunk.content}" for chunk in chunks]
    )
    manifest = IndexManifest(
        dimension=resolved.embedding_model.dimension,
        chunk_count=len(chunks),
        embedding_model=resolved.embedding_model.model,
        embedding_fingerprint=embedding_config_fingerprint(
            resolved.embedding_model
        ),
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
