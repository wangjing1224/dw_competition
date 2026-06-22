"""按章节和表格将统一解析结果切分为稳定索引片段。"""

import re

from app.models.documents import ParsedDocument
from app.retrieval.models import IndexChunk


TOKEN_PATTERN = re.compile(
    r"[\u4e00-\u9fff]|[A-Za-z0-9]+(?:[._/-][A-Za-z0-9]+)*|[^\s]"
)


def _tokenize(text: str) -> list[str]:
    return TOKEN_PATTERN.findall(text)


def split_with_overlap(
    text: str, max_tokens: int, overlap_tokens: int
) -> list[str]:
    if overlap_tokens >= max_tokens:
        raise ValueError("chunk_overlap 必须小于 chunk_tokens")
    tokens = _tokenize(text)
    if not tokens:
        return []
    chunks: list[str] = []
    step = max_tokens - overlap_tokens
    for start in range(0, len(tokens), step):
        current = tokens[start : start + max_tokens]
        if not current:
            break
        chunks.append(" ".join(current))
        if start + max_tokens >= len(tokens):
            break
    return chunks


def _chunk_id(
    file_id: str,
    section_index: int,
    unit_index: int,
    chunk_index: int,
) -> str:
    """生成紧凑位置 ID，避免每条检索结果携带长哈希占用上下文。"""

    file_number = file_id.removeprefix("file-")
    return (
        f"chunk-{file_number}-{section_index + 1}-"
        f"{unit_index + 1}-{chunk_index + 1}"
    )


def chunk_parsed_document(
    parsed: ParsedDocument,
    *,
    max_tokens: int,
    overlap_tokens: int,
) -> list[IndexChunk]:
    chunks: list[IndexChunk] = []
    document = parsed.document
    for section_index, section in enumerate(parsed.sections):
        units = [section.content, *section.tables]
        for unit_index, unit in enumerate(units):
            for chunk_index, text in enumerate(
                split_with_overlap(unit, max_tokens, overlap_tokens)
            ):
                if not text.strip():
                    continue
                chunks.append(
                    IndexChunk(
                        chunk_id=_chunk_id(
                            document.file_id,
                            section_index,
                            unit_index,
                            chunk_index,
                        ),
                        file_id=document.file_id,
                        project_id=document.project_id,
                        bidder_id=document.bidder_id,
                        task_id=document.task_id,
                        data_role=document.data_role,
                        file_name=document.file_name,
                        source_path=document.source_path,
                        section=section.title,
                        content=text,
                        parse_status=parsed.parse_status,
                        evidence_type=document.evidence_type,
                        classification_status=document.classification_status,
                    )
                )
    return chunks
