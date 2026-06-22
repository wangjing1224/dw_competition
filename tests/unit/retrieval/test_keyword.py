"""型号、包号和数值单位关键词保护测试。"""

from app.retrieval.keyword import keyword_tokens


def test_exact_identifiers_are_preserved():
    tokens = keyword_tokens("包1 型号 QW-640X 测温范围 650℃ 日期2026-04-21")

    assert "qw-640x" in tokens
    assert "650℃" in tokens
    assert "2026-04-21" in tokens
