"""解析器内部顺序 Block 和表格辅助模型。"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ParsedBlock:
    kind: Literal["paragraph", "table"]
    index: int
    text: str = ""
    heading_level: int | None = None
    table_markdown: str | None = None
    image_count: int = 0
