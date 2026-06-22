"""原子写入和加载 chunks、向量矩阵、关键词索引及 manifest。"""

from __future__ import annotations

import json
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import numpy as np

from app.core.config import get_settings
from app.retrieval.models import IndexChunk, IndexManifest


class IndexRebuildRequired(RuntimeError):
    """当前 Embedding 配置与已有索引不一致。"""


class CorruptedIndex(RuntimeError):
    """索引文件缺失或行数不一致。"""


class LoadedIndex:
    def __init__(
        self,
        manifest: IndexManifest,
        chunks: list[IndexChunk],
        vectors: np.ndarray,
        keyword_index: dict[str, list[int]],
    ) -> None:
        self.manifest = manifest
        self.chunks = chunks
        self.vectors = vectors
        self.keyword_index = keyword_index


def index_directory(scope_id: str) -> Path:
    if not scope_id or "/" in scope_id or "\\" in scope_id or ".." in scope_id:
        raise ValueError("scope_id 不安全")
    return get_settings().storage.data_root / "indexes" / scope_id


@contextmanager
def atomic_index_directory(scope_id: str) -> Iterator[Path]:
    target = index_directory(scope_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{scope_id}-", dir=target.parent)
    )
    backup = target.with_name(f".{target.name}-backup")
    try:
        yield temporary
        if backup.exists():
            shutil.rmtree(backup)
        if target.exists():
            target.replace(backup)
        temporary.replace(target)
        if backup.exists():
            shutil.rmtree(backup)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        if backup.exists() and not target.exists():
            backup.replace(target)
        raise


def write_index(
    directory: Path,
    manifest: IndexManifest,
    chunks: list[IndexChunk],
    vectors: np.ndarray,
    keyword_index: dict[str, list[int]],
) -> None:
    (directory / "index.json").write_text(
        manifest.model_dump_json(indent=2), encoding="utf-8"
    )
    with (directory / "chunks.jsonl").open("w", encoding="utf-8") as file:
        for chunk in chunks:
            file.write(chunk.model_dump_json())
            file.write("\n")
    np.save(directory / "embeddings.npy", vectors.astype(np.float32))
    (directory / "keyword_index.json").write_text(
        json.dumps(keyword_index, ensure_ascii=False),
        encoding="utf-8",
    )


def load_index(
    scope_id: str,
    *,
    expected_fingerprint: str,
) -> LoadedIndex:
    directory = index_directory(scope_id)
    manifest = IndexManifest.model_validate_json(
        (directory / "index.json").read_text("utf-8")
    )
    if manifest.embedding_fingerprint != expected_fingerprint:
        raise IndexRebuildRequired("Embedding 配置变化，需要重建索引")
    chunks = [
        IndexChunk.model_validate_json(line)
        for line in (directory / "chunks.jsonl").read_text("utf-8").splitlines()
        if line.strip()
    ]
    vectors = np.load(directory / "embeddings.npy", allow_pickle=False)
    keyword_index = json.loads(
        (directory / "keyword_index.json").read_text("utf-8")
    )
    if vectors.shape != (len(chunks), manifest.dimension):
        raise CorruptedIndex("向量矩阵行数或维度与索引清单不一致")
    if len(chunks) != manifest.chunk_count:
        raise CorruptedIndex("片段数量与索引清单不一致")
    return LoadedIndex(manifest, chunks, vectors, keyword_index)
