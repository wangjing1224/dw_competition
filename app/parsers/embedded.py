"""只读展开 Office 嵌入对象及其压缩包；不执行宏、不按附件路径写磁盘。"""

from __future__ import annotations

import hashlib
import logging
import shutil
import stat
import struct
import subprocess
import tempfile
from threading import Timer
from io import BytesIO
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

import olefile
import rarfile
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml.ns import qn

from app.core.config import get_settings
from app.models.documents import ParsedBlock, ParsedSection

logger = logging.getLogger(__name__)


class EmbeddedReadError(ValueError):
    """可以向原文读取者展示的附件降级原因。"""


def _native_payload(data: bytes) -> tuple[str, bytes]:
    """OLE Package 的 Ole10Native 包头；路径仅跳过，不用于读取本地文件。"""
    if len(data) < 6 or struct.unpack_from("<I", data)[0] > len(data) - 4:
        raise EmbeddedReadError("OLE 包长度不完整")
    position = 6

    def string():
        nonlocal position
        end = data.index(b"\0", position)
        value = data[position:end]
        position = end + 1
        return value

    raw_name = string()
    string()  # 原发送方磁盘路径，不读取、不展示。
    position += 8
    string()  # 原发送方临时路径。
    size = struct.unpack_from("<I", data, position)[0]
    payload = data[position + 4:position + 4 + size]
    if len(payload) != size:
        raise EmbeddedReadError("OLE 附件内容不完整")
    try:
        name = raw_name.decode("utf-8")
    except UnicodeDecodeError:
        name = raw_name.decode("gb18030", errors="replace")
    return name.replace("\\", "/").rsplit("/", 1)[-1], payload


class EmbeddedParser:
    def __init__(self, document, assets):
        self.document, self.assets = document, assets
        self.settings = get_settings().parsing
        self.count = self.total_bytes = 0

    def _reserve(self, size):
        if size < 0 or self.total_bytes + size > self.settings.max_embedded_bytes:
            raise EmbeddedReadError("嵌入附件累计展开大小超过解析上限")
        self.total_bytes += size

    def _warning(self, label, reason, warnings):
        message = f"嵌入附件 {label} 未展开：{reason}"
        warnings.append(message)
        return [self._section(label, [ParsedBlock(
            kind="text", content=f"[{message}]", source_block_index=0,
        )])]

    def _section(self, title, blocks):
        return ParsedSection(
            section_id="embedded-" + hashlib.sha256(title.encode()).hexdigest()[:20],
            title=title, blocks=blocks,
        )

    def object(self, element, part, warnings, *, depth=0):
        label = "未定位对象"
        try:
            relation = part.rels.get(element.get(qn("r:id")))
            if relation is None:
                raise EmbeddedReadError("嵌入对象关系缺失")
            if relation.is_external or element.get("Type") == "Link":
                raise EmbeddedReadError("对象为外部链接，未自动下载")
            if relation.reltype not in {RT.OLE_OBJECT, RT.PACKAGE}:
                raise EmbeddedReadError("对象关系不是嵌入文件")
            label = str(relation.target_part.partname).lstrip("/")
            return self.read(relation.target_part.blob, label, warnings, depth=depth)
        except EmbeddedReadError as exc:
            return self._warning(label, str(exc), warnings)
        except Exception as exc:
            logger.warning("嵌入对象关系读取失败", exc_info=True)
            return self._warning(label, type(exc).__name__, warnings)

    def read(self, data, label, warnings, *, depth=0):
        try:
            if depth >= self.settings.max_embedded_depth:
                raise EmbeddedReadError("嵌套深度超过解析上限")
            self.count += 1
            if self.count > self.settings.max_embedded_files:
                raise EmbeddedReadError("嵌入附件数量超过解析上限")
            self._reserve(len(data))
            if data.startswith(olefile.MAGIC):
                with olefile.OleFileIO(BytesIO(data)) as ole:
                    if ole.exists("Package"):
                        return self.read(ole.openstream("Package").read(), label, warnings, depth=depth + 1)
                    if ole.exists("\x01Ole10Native"):
                        name, payload = _native_payload(ole.openstream("\x01Ole10Native").read())
                        return self.read(payload, label + " > " + name, warnings, depth=depth + 1)
                    if ole.exists("Workbook") or ole.exists("Book"):
                        return self._office(data, ".xls", label, warnings, depth)
                    raise EmbeddedReadError("暂不支持此 OLE 对象类型")
            if data.startswith(b"PK\x03\x04"):
                with ZipFile(BytesIO(data)) as archive:
                    names = set(archive.namelist())
                    if "word/document.xml" in names or "xl/workbook.xml" in names:
                        self._reserve(sum(i.file_size for i in archive.infolist()))
                        suffix = ".docx" if "word/document.xml" in names else ".xlsx"
                        return self._office(data, suffix, label, warnings, depth)
                    return self._archive(archive, label, warnings, depth, rar=False)
            if data.startswith(b"Rar!\x1a\x07"):
                with rarfile.RarFile(BytesIO(data)) as archive:
                    return self._archive(archive, label, warnings, depth, rar=True, data=data)
            raise EmbeddedReadError("暂不支持此附件格式，未将图标视为正文")
        except EmbeddedReadError as exc:
            return self._warning(label, str(exc), warnings)
        except Exception as exc:
            logger.warning("嵌入附件解析失败: %s", label, exc_info=True)
            return self._warning(label, type(exc).__name__ + "，请回查原附件", warnings)

    def _office(self, data, suffix, label, warnings, depth):
        from app.parsers.docx_parser import DocxParser
        from app.parsers.xlsx_parser import XlsParser, XlsxParser

        # 原文件名从不作为临时文件路径，且子附件继续使用父文件的图片资产和授权身份。
        with tempfile.TemporaryDirectory(prefix="embedded-") as folder:
            path = Path(folder) / ("payload" + suffix)
            path.write_bytes(data)
            if suffix == ".docx":
                result = DocxParser().parse(
                    self.document, path, assets=self.assets,
                    _embedded=self, _depth=depth + 1,
                )
            else:
                parser = XlsxParser() if suffix == ".xlsx" else XlsParser()
                result = parser.parse(self.document, path, assets=self.assets)
        warnings.extend(f"嵌入附件 {label}：{w}" for w in result.warnings)
        if not result.sections:
            return self._warning(label, "未取得可读取正文", warnings)
        for section in result.sections:
            section.title = f"嵌入附件 {label} > {section.title}"
        return result.sections

    def _archive(self, archive, label, warnings, depth, *, rar, data=None):
        sections = []
        for info in archive.infolist():
            if (info.isdir() if rar else info.is_dir()):
                continue
            name = info.filename.replace("\\", "/")
            child_label = label + " > " + name
            try:
                if (PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts
                        or ":" in name or "\n" in name):
                    raise EmbeddedReadError("不安全的压缩包成员路径")
                if (rar and info.is_symlink()) or (
                    not rar and stat.S_ISLNK(info.external_attr >> 16)
                ):
                    raise EmbeddedReadError("不展开压缩包中的链接")
                if (rar and info.needs_password()) or (not rar and info.flag_bits & 1):
                    raise EmbeddedReadError("附件已加密，未自动尝试密码")
                if self.count >= self.settings.max_embedded_files:
                    raise EmbeddedReadError("嵌入附件数量超过解析上限，其余成员未展开")
                if info.file_size < 0 or self.total_bytes + info.file_size > self.settings.max_embedded_bytes:
                    raise EmbeddedReadError("嵌入附件累计展开大小超过解析上限")
                payload = self._rar_read(archive, info, data) if rar else archive.read(info)
                sections.extend(self.read(payload, child_label, warnings, depth=depth + 1))
            except Exception as exc:
                reason = str(exc) if isinstance(exc, EmbeddedReadError) else type(exc).__name__
                sections.extend(self._warning(child_label, reason, warnings))
                if self.count >= self.settings.max_embedded_files:
                    break
        return sections or self._warning(label, "压缩包没有可读取的文件", warnings)

    @staticmethod
    def _rar_read(archive, info, data):
        # rarfile 的 bsdtar 命令在部分版本中将 -- 放到 -f 后，导致把 -- 当文件名。
        # 有 bsdtar 时直接以正确参数只读指定成员；不 extractall、不执行成员。
        executable = shutil.which("bsdtar")
        if executable is None:
            try:
                return archive.read(info)  # rarfile 可使用 unrar/unar/7zip。
            except rarfile.RarCannotExec as exc:
                raise EmbeddedReadError("RAR 解压器不可用，请安装 bsdtar、unrar、unar 或 7-Zip") from exc
        with tempfile.TemporaryDirectory(prefix="embedded-rar-") as folder:
            path = Path(folder) / "payload.rar"
            path.write_bytes(data)
            with subprocess.Popen(
                    [executable, "-xOf", str(path), "--", info.filename],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            ) as process:
                # 限时且只读取声明长度+1；异常输出不会无限写入内存或临时磁盘。
                timeout = Timer(30, process.kill)
                timeout.daemon = True
                timeout.start()
                try:
                    payload = process.stdout.read(info.file_size + 1)
                    if len(payload) > info.file_size:
                        process.kill()
                    returncode = process.wait()
                finally:
                    if process.poll() is None:
                        process.kill()
                    timeout.cancel()
                if returncode or len(payload) != info.file_size:
                    raise EmbeddedReadError("RAR 解压失败或内容长度不符，请检查解压器支持")
                return payload
