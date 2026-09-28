"""来源读取公共方法：调用方必须先校验业务归属，不接受任意磁盘路径。"""

import re
from pathlib import PurePosixPath

from app.core.errors import BusinessError, NotFoundError
from app.models.documents import ParsedDocument
from app.parsers.assets import AssetStore, file_asset_root


class SourcePreviewService:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store

    def parsed(self, file, version=None):
        file_id = file.file_id
        root = file_asset_root(self.settings.storage.data_root, file_id)
        if version and not re.fullmatch(r"[a-f0-9]{64}", version):
            raise BusinessError(
                "invalid_parse_version", "解析版本不合法", http_status=422
            )
        path = (
            root / "versions" / version / "parsed.json"
            if version
            else root.with_suffix(".json")
        )
        if not path.exists():
            if version:
                raise NotFoundError("parse_version_not_found", "解析版本不存在")
            raise BusinessError(
                "preview_not_ready", "此文件暂无可用预览", http_status=409
            )
        try:
            parsed = ParsedDocument.model_validate_json(path.read_bytes())
            if (
                parsed.document.file_id != file_id
                or parsed.document.project_id != file.project_id
                or parsed.document.bidder_id != file.bidder_id
                or parsed.document.task_id != file.task_id
                or parsed.document.data_role != file.data_role
            ):
                raise ValueError("解析归属不符")
            if not parsed.markdown_path or not re.fullmatch(
                re.escape(file_id) + r"/versions/[a-f0-9]{64}/document.md",
                parsed.markdown_path,
            ):
                raise ValueError("解析 Markdown 路径不合法")
            if version and parsed.markdown_path.split("/")[-2] != version:
                raise ValueError("解析版本不符")
            return file, parsed
        except (OSError, ValueError) as exc:
            raise BusinessError(
                "preview_unavailable", "解析预览不可读取", http_status=500
            ) from exc

    def preview(self, file, base_url, version=None):
        file_id = file.file_id
        file, parsed = self.parsed(file, version)
        version = parsed.markdown_path.split("/")[-2]
        try:
            markdown = self.store.resolve_relative(
                "parsed/" + parsed.markdown_path
            ).read_text("utf-8")
        except OSError as exc:
            raise BusinessError(
                "preview_unavailable", "解析正文不可读取", http_status=500
            ) from exc
        images = {
            image.path: image
            for s in parsed.sections
            for b in s.blocks
            for image in b.images
        }
        return {
            "file_id": file_id,
            "file_name": file.file_name,
            "source_path": file.source_path,
            "parse_status": parsed.parse_status,
            "parse_version": version,
            "markdown": markdown,
            "warnings": parsed.warnings,
            "images": [
                {
                    "path": image.path,
                    "url": f"{base_url}?image_name={PurePosixPath(image.path).name}&version={version}",
                    "purpose": image.purpose,
                    "read_status": image.read_status,
                    "description": image.description,
                }
                for image in images.values()
            ],
        }

    def image(self, file, image_name, version):
        file_id = file.file_id
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.(?:png|jpe?g)", image_name):
            raise BusinessError("invalid_image_name", "图片名称不合法", http_status=422)
        _, parsed = self.parsed(file, version)
        path = f"{file_id}/images/{image_name}"
        image = next(
            (
                image
                for s in parsed.sections
                for b in s.blocks
                for image in b.images
                if image.path == path
            ),
            None,
        )
        if image is None:
            raise NotFoundError("image_not_found", "图片未在此解析版本中登记")
        try:
            data = AssetStore(
                file_asset_root(self.settings.storage.data_root, file_id)
            ).read_image(image)
            return data, image.mime_type
        except (OSError, ValueError) as exc:
            raise BusinessError(
                "image_unavailable", "已登记图片无法读取", http_status=500
            ) from exc
