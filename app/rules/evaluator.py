"""数值、日期、列表和文本字段的确定性归一化与比较。"""

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.models.common import ComparisonResult


NUMBER_PATTERN = re.compile(r"[-+]?\d+(?:\.\d+)?")
UNIT_FACTORS = {
    "mm": Decimal("0.001"),
    "cm": Decimal("0.01"),
    "m": Decimal("1"),
    "kv": Decimal("1000"),
    "v": Decimal("1"),
    "ma": Decimal("0.001"),
    "a": Decimal("1"),
}


def _number(value: Any, target_unit: str | None = None) -> Decimal:
    text = str(value).strip().lower()
    matched = NUMBER_PATTERN.search(text)
    if not matched:
        raise ValueError("没有可解析数值")
    number = Decimal(matched.group())
    unit_match = re.search(r"(mm|cm|m|kv|v|ma|a)\b", text)
    source_unit = unit_match.group(1) if unit_match else None
    raw_unit = re.search(r"[-+]?\d+(?:\.\d+)?\s*([a-zA-Z℃°]+)", text)
    if raw_unit and source_unit is None:
        raise ValueError(f"不支持的单位: {raw_unit.group(1)}")
    if target_unit and source_unit:
        if source_unit not in UNIT_FACTORS or target_unit.lower() not in UNIT_FACTORS:
            raise ValueError("不支持的单位")
        number = number * UNIT_FACTORS[source_unit] / UNIT_FACTORS[target_unit.lower()]
    return number


def _date(value: Any) -> date:
    text = str(value).strip().replace("年", "-").replace("月", "-").replace("日", "")
    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y/%m"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise ValueError("日期格式无法识别")


def normalize_value(value: Any, value_type: str, unit: str | None = None) -> Any:
    if value_type == "number":
        return _number(value, unit)
    if value_type == "date":
        return _date(value)
    if value_type == "list":
        if isinstance(value, list):
            return [str(item).strip().lower() for item in value]
        return [item.strip().lower() for item in re.split(r"[,，;；\n]", str(value)) if item.strip()]
    if value_type in {"text", "enum", "model_no", "package_no"}:
        return re.sub(r"\s+", "", str(value)).lower()
    return value


def compare_values(
    operator: str,
    expected: Any,
    actual: Any,
    value_type: str,
    unit: str | None = None,
) -> ComparisonResult:
    """无法可靠归一化时返回 manual_review，不猜测单位或日期。"""

    if actual is None or actual == "":
        return ComparisonResult(status="missing", raw_expected=expected, raw_actual=actual)
    try:
        normalized_expected = normalize_value(expected, value_type, unit)
        normalized_actual = normalize_value(actual, value_type, unit)
    except (ValueError, InvalidOperation) as exc:
        return ComparisonResult(
            status="manual_review",
            raw_expected=expected,
            raw_actual=actual,
            message=str(exc),
        )

    if operator == ">=":
        matched = normalized_actual >= normalized_expected
    elif operator == "<=":
        matched = normalized_actual <= normalized_expected
    elif operator in {"=", "==", "equals"}:
        matched = normalized_actual == normalized_expected
    elif operator == "contains":
        matched = normalized_expected in normalized_actual
    elif operator == "contains_all":
        matched = set(normalized_expected) <= set(normalized_actual)
    elif operator == "date_before":
        matched = normalized_actual <= normalized_expected
    else:
        return ComparisonResult(
            status="unsupported_operator",
            raw_expected=expected,
            raw_actual=actual,
        )
    return ComparisonResult(
        status="matched" if matched else "not_matched",
        raw_expected=expected,
        raw_actual=actual,
        normalized_expected=normalized_expected,
        normalized_actual=normalized_actual,
    )
