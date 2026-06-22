"""确定性数值、日期、列表和缺失值比较测试。"""

from app.rules.evaluator import compare_values


def test_number_unit_conversion():
    result = compare_values(">=", "1m", "1200mm", "number", unit="m")
    assert result.status == "matched"


def test_missing_value_is_not_guessed():
    assert compare_values("equals", "包1", "", "package_no").status == "missing"


def test_unknown_unit_requires_manual_review():
    result = compare_values(">=", "1m", "2kg", "number", unit="m")
    assert result.status == "manual_review"
