"""按原生有序结构建索引；保留表格完整性及真实图片/页面/解析版本关联。"""

import hashlib
import re

from app.models.documents import ParsedDocument
from app.retrieval.models import IndexChunk

TOKEN_PATTERN = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9]+(?:[._/-][A-Za-z0-9]+)*|[^\s]")


def _tokenize(text: str) -> list[str]:
    return TOKEN_PATTERN.findall(text)


def split_with_overlap(text: str, max_tokens: int, overlap_tokens: int) -> list[str]:
    if max_tokens <= 0 or overlap_tokens < 0 or overlap_tokens >= max_tokens:
        raise ValueError("chunk_overlap 必须非负且小于 chunk_tokens")
    tokens = list(TOKEN_PATTERN.finditer(text))
    chunks = []
    step = max_tokens - overlap_tokens
    for start in range(0, len(tokens), step):
        end = min(start + max_tokens, len(tokens))
        # 保留原文空格、标点和换行；不能用空格重组分词结果冒充原文。
        chunks.append(text[tokens[start].start() : tokens[end - 1].end()])
        if end == len(tokens):
            break
    return chunks


def _chunk_id(file_id, section_index, unit_index, chunk_index):
    return f"chunk-{file_id.removeprefix('file-')}-{section_index + 1}-{unit_index + 1}-{chunk_index + 1}"


def chunk_parsed_document(
    parsed: ParsedDocument,
    *,
    max_tokens: int,
    overlap_tokens: int,
    retrieval_hint: str | None = None,
) -> list[IndexChunk]:
    chunks = []
    document = parsed.document
    version = parsed.markdown_path
    # 新结构重建时使用版本化片段标识，不能让旧报告的 ID 指向不同位置。
    fingerprint = hashlib.sha256(
        (version or parsed.parser_version or "blocks").encode()
    ).hexdigest()[:8]
    for section_index, section in enumerate(parsed.sections):
        if parsed.schema_version == 2:
            units = []
            for block in section.blocks:
                body = "\n\n".join(
                    part
                    for part in [
                        block.content,
                        *(
                            image.transcription
                            for image in block.images
                            if image.purpose == "content"
                        ),
                    ]
                    if part.strip()
                )
                units.append(
                    (
                        body,
                        block.kind,
                        block.source_block_index,
                        sorted({source.page_number for source in block.page_sources}),
                        [image.path for image in block.images],
                    )
                )
        else:
            # 仅为尚未切换的其他流程读取；规则 Agent 明确要求有序结构。
            units = [(section.content, "text", None, [], [])]
            units.extend((table, "table", None, [], []) for table in section.tables)
        for unit_index, (body, kind, block_index, pages, images) in enumerate(units):
            # 复杂表格不按字符或 Token 拆断；图片的结构化转写同样完整保留。
            texts = (
                split_with_overlap(body, max_tokens, overlap_tokens)
                if kind == "text"
                else [body]
            )
            for chunk_index, text in enumerate(texts):
                if not text.strip():
                    continue
                key = _chunk_id(
                    document.file_id, section_index, unit_index, chunk_index
                )
                if parsed.schema_version == 2:
                    key += f"-v{fingerprint}"
                chunks.append(
                    IndexChunk(
                        chunk_id=key,
                        file_id=document.file_id,
                        project_id=document.project_id,
                        bidder_id=document.bidder_id,
                        task_id=document.task_id,
                        data_role=document.data_role,
                        file_name=document.file_name,
                        source_path=document.source_path,
                        section=section.title,
                        content=text,
                        retrieval_hint=retrieval_hint,
                        parse_status=parsed.parse_status,
                        evidence_type=document.evidence_type,
                        classification_status=document.classification_status,
                        block_kind=kind,
                        source_block_index=block_index,
                        parse_version=version,
                        page_numbers=pages,
                        image_paths=images,
                    )
                )
    return chunks
