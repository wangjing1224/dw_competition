"""旧版 DOC 转换能力检测和明确降级。"""

import shutil
import subprocess
import tempfile
from pathlib import Path

from app.models.documents import DocumentRef, ParsedDocument
from app.parsers.common import finish_document
from app.parsers.docx_parser import DocxParser


class LegacyDocParser:
    def supports(self, suffix: str) -> bool:
        return suffix.lower() == ".doc"

    def parse(self, document: DocumentRef, stored_path: Path) -> ParsedDocument:
        executable = shutil.which("libreoffice") or shutil.which("soffice")
        if executable is None:
            return ParsedDocument(
                document=document.model_copy(update={"parse_status": "unsupported"}),
                parse_status="unsupported",
                warnings=["DOC 需要安装 LibreOffice 后转换为 DOCX"],
            )
        with tempfile.TemporaryDirectory() as temporary:
            subprocess.run(
                [
                    executable,
                    f"-env:UserInstallation={Path(temporary).as_uri()}/profile",
                    "--headless",
                    "--convert-to",
                    "docx",
                    "--outdir",
                    temporary,
                    str(stored_path),
                ],
                check=True,
                capture_output=True,
                timeout=120,
            )
            converted = Path(temporary) / f"{stored_path.stem}.docx"
            result = DocxParser().parse(document, converted)
            # 内容来自转换文件，缓存身份仍绑定用户上传的原始 DOC。
            return finish_document(
                document, stored_path, result.sections, result.warnings
            )
