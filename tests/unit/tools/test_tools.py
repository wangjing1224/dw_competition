"""BaseTool 名称、Schema 和资料归属约束测试。"""

import pytest
from pydantic import ValidationError

from app.tools.bid_tools import SearchBidResponseInput
from app.tools.evidence_tools import SearchEvidenceInput


def test_bid_tool_schema_rejects_extra_fields():
    with pytest.raises(ValidationError):
        SearchBidResponseInput.model_validate(
            {
                "task_id": "audit-1",
                "bid_file_id": "file-1",
                "rule": {},
                "unexpected": True,
            }
        )


def test_evidence_tool_requires_claim():
    with pytest.raises(ValidationError):
        SearchEvidenceInput.model_validate({})
