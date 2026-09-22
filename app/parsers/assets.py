"""文件级图片资产与安全读取。只处理字节，不调用模型。"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import io
import json
import os
import re
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

from app.models.documents import ParsedDocument, ParsedImage, validate_asset_path


def file_asset_root(data_root: Path, file_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", file_id):
        raise ValueError("不安全的 file_id")
    root = (data_root / "parsed").resolve()
    target = (root / file_id).resolve()
    if target.parent != root or target.name != file_id:
        raise ValueError("文件资源目录越界")
    return target


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".writing-", delete=False
        ) as out:
            name = out.name
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


class AssetStore:
    def __init__(
        self, root: Path, *, max_bytes: int = 20_000_000, max_pixels: int = 40_000_000
    ):
        self.root = root.resolve()
        self.max_bytes = max_bytes
        self.max_pixels = max_pixels

    def save_image(
        self, data: bytes, *, purpose="content", description=""
    ) -> ParsedImage:
        if len(data) > self.max_bytes:
            raise ValueError("图片字节数超限")
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > self.max_pixels:
                raise ValueError("图片像素数超限")
            if getattr(source, "n_frames", 1) != 1:
                raise ValueError("多帧图片暂不支持，不能只取首帧冒充完整图片")
            source.load()  # 在写入前真正解码；不相信扩展名和声明的 MIME。
            oriented = ImageOps.exif_transpose(source).convert("RGBA")
            # 文档透明图片按白底合成，不能直接丢掉 alpha 导致黑字变黑底。
            image = Image.new("RGB", oriented.size, "white")
            image.paste(oriented, mask=oriented.getchannel("A"))
            buf = io.BytesIO()
            image.save(buf, format="PNG")
        payload = buf.getvalue()
        if len(payload) > self.max_bytes:
            raise ValueError("规范化图片字节数超限")
        digest = hashlib.sha256(payload).hexdigest()
        relative = self._register_image(digest, payload)
        return ParsedImage(
            path=relative,
            mime_type="image/png",
            width=image.width,
            height=image.height,
            purpose=purpose,
            description=description,
        )

    def _register_image(self, digest: str, payload: bytes) -> str:
        """文件内短名。锁覆盖分配与发布，进程中断留下的编号不覆盖、不复用。"""
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".assets.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            manifest = self.root / "assets.json"
            state = (
                json.loads(manifest.read_text("utf-8"))
                if manifest.exists()
                else {"last_id": 0, "images": {}}
            )
            relative = state["images"].get(digest)
            if relative:
                # 只在重新解析并复用旧资产时升级登记路径，不移动图片或改写旧快照。
                canonical = (
                    f"{self.root.name}/{relative}"
                    if relative.startswith("images/")
                    else relative
                )
                path = self.resolve_path(canonical)
                # 资产缺失或被篡改不能静默复用损坏链接。
                if path.exists():
                    if path.read_bytes() != payload:
                        raise ValueError("已登记图片内容与哈希不一致")
                    if canonical != relative:
                        state["images"][digest] = canonical
                        atomic_write(manifest, json.dumps(state, sort_keys=True).encode())
                    return canonical
            existing = [
                int(p.stem)
                for p in (self.root / "images").glob("*.png")
                if p.stem.isdigit()
            ]
            number = max([state["last_id"], *existing]) + 1
            relative = f"{self.root.name}/images/{number}.png"
            path = self.resolve_path(relative)
            # 先保留序号；崩溃最多跳号，不让旧版本指向新内容。
            state["last_id"] = number
            atomic_write(manifest, json.dumps(state, sort_keys=True).encode())
            atomic_write(path, payload)
            state["images"][digest] = relative
            atomic_write(manifest, json.dumps(state, sort_keys=True).encode())
            return relative

    def resolve_path(self, relative: str) -> Path:
        """将模型可见的完整资源路径解析到当前文件；禁止跨文件和软链越界。"""
        validate_asset_path(relative)
        if relative.split("/", 1)[0] != self.root.name:
            raise ValueError("图片路径的 file_id 与当前文件资源目录不一致")
        path = (self.root.parent / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("图片路径越过当前文件资源目录")
        return path

    def read_image(self, image: ParsedImage) -> bytes:
        with self.resolve_path(image.path).open("rb") as stream:
            data = stream.read(self.max_bytes + 1)
        if len(data) > self.max_bytes:
            raise ValueError("图片字节数超限")
        with Image.open(io.BytesIO(data)) as decoded:
            expected = {"image/png": "PNG", "image/jpeg": "JPEG"}[image.mime_type]
            if decoded.format != expected or decoded.size != (
                image.width,
                image.height,
            ):
                raise ValueError("图片实际格式或尺寸与元数据不一致")
            if decoded.width * decoded.height > self.max_pixels:
                raise ValueError("图片像素数超限")
            decoded.load()
        return data


def build_image_content(
    parsed: ParsedDocument, image_path: str, assets: AssetStore, *, max_side: int = 2048
) -> dict:
    """调用方先授权文件范围；这里只允许读取已登记在该文件中的图片。"""
    if assets.root.name != parsed.document.file_id:
        raise ValueError("图片资源目录与当前文件身份不一致")
    if max_side < 1:
        raise ValueError("图片尺寸上限必须为正数")
    image = next(
        (
            i
            for s in parsed.sections
            for b in s.blocks
            for i in b.images
            if i.path == image_path
        ),
        None,
    )
    if image is None:
        raise ValueError("当前解析文档未登记此图片")
    data = assets.read_image(image)
    with Image.open(io.BytesIO(data)) as decoded:
        if max(decoded.size) > max_side:
            decoded.thumbnail((max_side, max_side))
            output = io.BytesIO()
            decoded.save(output, format="PNG")
            data = output.getvalue()
            mime = "image/png"
        else:
            mime = image.mime_type
    encoded = base64.b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}
