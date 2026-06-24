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
from app.evidence.storage import load_evidence_classification


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
        # 解析 JSON 中的 parse_status 是解析完成后的最新值；资料分类则发生在
        # 解析之后，因此只覆盖分类字段，不能用上传阶段的 pending 状态回退它。
        indexed_document = parsed.document.model_copy(
            update={
                "evidence_type": document.evidence_type,
                "classification_status": document.classification_status,
            }
        )
        parsed = parsed.model_copy(update={"document": indexed_document})
        retrieval_hint = None
        if str(document.data_role) == "evidence_material":
            classification = load_evidence_classification(
                document.file_id,
                resolved,
            )
            if classification is not None:
                retrieval_hint = classification.evidence_summary
        chunks.extend(
            chunk_parsed_document(
                parsed,
                max_tokens=resolved.retrieval.chunk_tokens,
                overlap_tokens=resolved.retrieval.chunk_overlap,
                retrieval_hint=retrieval_hint,
            )
        )
    vectors = await embedding_model.embed(
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
