"""旧版 DOC 转换能力检测和明确降级。"""

import shutil
import subprocess
import tempfile
from pathlib import Path

from app.models.documents import DocumentRef, ParsedDocument
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
                    "--headless",
                    "--convert-to",
                    "docx",
                    "--outdir",
                    temporary,
                    str(stored_path),
                ],
                check=True,
                capture_output=True,
            )
            converted = Path(temporary) / f"{stored_path.stem}.docx"
            return DocxParser().parse(document, converted)
