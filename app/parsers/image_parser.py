"""独立图片只提取资产；视觉转写由异步公共解析服务按需执行。"""

from pathlib import Path

from app.models.documents import DocumentRef, ParsedBlock, ParsedSection
from app.parsers.assets import AssetStore
from app.parsers.common import assets_for, finish_document
from app.parsers.docx_parser import stable_section_id


class ImageParser:
    def supports(self, suffix: str) -> bool:
        return suffix.lower() in {".png", ".jpg", ".jpeg"}

    def parse(
        self,
        document: DocumentRef,
        stored_path: Path,
        *,
        assets: AssetStore | None = None,
    ):
        assets = assets or assets_for(document)
        with stored_path.open("rb") as stream:
            data = stream.read(assets.max_bytes + 1)
        image = assets.save_image(data)
        section = ParsedSection(
            section_id=stable_section_id(document.file_id, 0, "图片"),
            title="图片",
            blocks=[ParsedBlock(kind="image", images=[image], source_block_index=0)],
        )
        return finish_document(document, stored_path, [section])
