"""解析器统一接口和可恢复解析异常。"""

from pathlib import Path
from typing import Protocol

from app.models.documents import DocumentRef, ParsedDocument


class DocumentParser(Protocol):
    def supports(self, suffix: str) -> bool: ...

    def parse(self, document: DocumentRef, stored_path: Path) -> ParsedDocument: ...


class ParseError(Exception):
    """无法形成可靠解析结果的基础异常。"""


class RecoverableParseError(ParseError):
    """解析部分失败，但已经得到可用文本。"""

    def __init__(self, message: str, partial_result: ParsedDocument) -> None:
        super().__init__(message)
        self.partial_result = partial_result
