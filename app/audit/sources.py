"""单文件的实际读取登记；不是从模型散文反向推断引用。"""

from app.models.results import ReadSource


class SourceLedger:
    def __init__(self, parsed_documents):
        self.documents = dict(parsed_documents)
        self.sources: dict[str, ReadSource] = {}

    def _source(self, parsed, **values):
        document = parsed.document
        return ReadSource(
            file_id=document.file_id,
            file_name=document.file_name,
            source_path=document.source_path,
            data_role=document.data_role,
            parse_version=parsed.markdown_path or "",
            **values,
        )

    def chunk_read(self, result):
        parsed = self.documents[result.source.file_id]
        if not parsed.markdown_path or result.parse_version != parsed.markdown_path:
            raise ValueError("来源解析版本与会话快照不一致")
        self.sources[result.chunk_id] = self._source(
            parsed,
            section=result.source.section,
            page_numbers=result.page_numbers,
            quote=result.content,
        )

    def image_read(self, path):
        parsed = self.documents[path.split("/", 1)[0]]
        for section in parsed.sections:
            for block in section.blocks:
                if any(image.path == path for image in block.images):
                    self.sources[path] = self._source(
                        parsed,
                        section=section.title,
                        page_numbers=sorted(
                            {p.page_number for p in block.page_sources}
                        ),
                    )
                    return
        raise ValueError("图片未登记在解析快照")

    def validate(self, key, role=None):
        source = self.sources.get(key)
        if source is None or (role is not None and source.data_role != role):
            raise ValueError(f"引用未实际读取、类型不符或越权：{key}")
        return source
