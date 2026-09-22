"""续表局部操作：模型只决定关系，原文单元格由代码按顺序保留。"""

import re
from html import unescape


class BoundaryError(ValueError):
    """只携带固定诊断，不把模型响应/敏感原文写进错误日志。"""

    def __init__(self, code: str, reason: str):
        self.code = code
        self.reason = reason
        super().__init__(reason)


def text_value(cell: str) -> str:
    return re.sub(r"\s+", "", unescape(re.sub(r"<[^>]+>", "", cell)))


def table_rows(html: str) -> list[list[str]]:
    # 输入先经 TableHTML 校验；目前局部操作只支持无跨行/跨列的矩形网格。
    rows = []
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", html, re.S):
        cells = []
        for attrs, body in re.findall(r"<t[dh]\b([^>]*)>(.*?)</t[dh]>", row, re.S):
            spans = re.findall(r"(?:rowspan|colspan)=[\"\'](\d+)[\"\']", attrs)
            if any(int(value) != 1 for value in spans):
                raise BoundaryError(
                    "complex_span", "边界表格含跨行或跨列，当前无法安全执行局部拼接"
                )
            cells.append(body)
        rows.append(cells)
    if not rows or not rows[0] or len({len(row) for row in rows}) != 1:
        raise BoundaryError("grid_mismatch", "表格列数不一致")
    return rows


def rows_html(rows: list[list[str]]) -> str:
    return (
        "<table>\n"
        + "\n".join(
            "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
            for row in rows
        )
        + "\n</table>"
    )


def boundary_view(html: str, *, tail: bool) -> str:
    """表头及边界各至多三行；不发送累计整表，明确中间行没有展示。"""
    rows = table_rows(html)
    selected = list(range(min(3, len(rows))))
    edge = range(max(0, len(rows) - 3), len(rows)) if tail else range(min(6, len(rows)))
    selected = sorted(set([*selected, *edge]))
    return "\n".join(f"原表第 {i + 1} 行：" + rows_html([rows[i]]) for i in selected)


def apply_boundary(
    left: str, right: str, *, drop_header_rows: int, join_boundary_row: bool
) -> str:
    a, b = table_rows(left), table_rows(right)
    if len(a[0]) != len(b[0]):
        raise BoundaryError("column_mismatch", "相邻表格列数不同，未执行拼接")
    n = drop_header_rows
    if n < 0 or n >= min(len(a), len(b)):
        raise BoundaryError("header_range", "重复表头行数越界或没有剩余数据行")
    for index in range(n):
        header = list(map(text_value, a[index]))
        if header != list(map(text_value, b[index])) or not any(header):
            raise BoundaryError(
                "header_mismatch", "拟删除的行与前表表头不一致或为空白行"
            )
        # 同内容多次出现时无法仅凭文字确定是表头还是重复数据，不冒险去重。
        if any(header == list(map(text_value, row)) for row in a[n:]):
            raise BoundaryError(
                "ambiguous_header", "拟删除表头与数据行重复，需回查确认"
            )
    remaining = b[n:]
    if join_boundary_row:
        x, y = a[-1], remaining[0]
        if not any(map(text_value, y)):
            raise BoundaryError("empty_boundary", "下一页边界行为空，不能作为续行删除")
        if text_value(x[0]) and text_value(y[0]):
            raise BoundaryError(
                "ambiguous_boundary", "两侧首列都有内容，当前无法安全判断为同一数据行"
            )
        if any(text_value(v) and text_value(v) == text_value(w) for v, w in zip(x, y)):
            raise BoundaryError(
                "repeated_boundary_value", "边界两侧存在重复字段，不能直接拼接或去重"
            )
        # 不重写、排序或猜测字段。换行仍保留，数字/文字片段保持同一单元格归属。
        a[-1] = [v + ("<br>" if v and w else "") + w for v, w in zip(x, y)]
        remaining = remaining[1:]
    return rows_html([*a, *remaining])
