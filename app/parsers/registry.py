"""按扩展名选择文档解析器。"""

from app.parsers.base import DocumentParser
from app.parsers.doc_converter import LegacyDocParser
from app.parsers.docx_parser import DocxParser
from app.parsers.text_parser import TextParser
from app.parsers.xlsx_parser import XlsxParser


class ParserRegistry:
    def __init__(self, parsers: list[DocumentParser] | None = None) -> None:
        self.parsers = parsers or [
            DocxParser(),
            XlsxParser(),
            TextParser(),
            LegacyDocParser(),
        ]

    def get(self, suffix: str) -> DocumentParser | None:
        return next(
            (parser for parser in self.parsers if parser.supports(suffix)),
            None,
        )


parser_registry = ParserRegistry()
