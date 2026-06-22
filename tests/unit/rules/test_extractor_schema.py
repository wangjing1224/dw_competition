"""规则抽取批次对常见模型结构偏差的兼容测试。"""

import pytest
from pydantic import ValidationError

from app.rules.extractor import RuleExtractionBatch


def test_flattened_project_profile_is_moved_under_profile():
    batch = RuleExtractionBatch.model_validate(
        {
            "project_name": "示例项目",
            "buyer": "示例采购人",
            "material_category": "物资类",
            "bid_deadline": "2026-04-21 09:30",
            "bid_validity_days": None,
            "rules": [],
        }
    )

    assert batch.project_profile.project_name == "示例项目"
    assert batch.project_profile.buyer == "示例采购人"
    assert batch.rules == []


def test_unknown_root_field_is_still_rejected():
    with pytest.raises(ValidationError):
        RuleExtractionBatch.model_validate(
            {
                "project_profile": {},
                "rules": [],
                "unexpected": "不能静默忽略",
            }
        )
