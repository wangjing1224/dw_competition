"""共享严格模型、枚举、ID 和确定性比较结果。"""

from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        use_enum_values=True,
    )

    @model_validator(mode="after")
    def reject_blank_identifiers(self) -> "StrictModel":
        # 使用模型级后置校验，避免干扰判别联合的 action 字段生成。
        for field_name in self.__class__.model_fields:
            if not field_name.endswith("_id"):
                continue
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ValueError(f"{field_name} 不能为空")
        return self


def generate_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


class DataRole(StrEnum):
    RULE_SOURCE = "rule_source"
    EVIDENCE_MATERIAL = "evidence_material"
    BID_DOCUMENT = "bid_document"


class ParseStatus(StrEnum):
    PENDING = "pending"
    PARSED = "parsed"
    PARTIAL = "partial"
    OCR_REQUIRED = "ocr_required"
    UNSUPPORTED = "unsupported"
    MANUAL_REVIEW = "manual_review"


class EvidenceType(StrEnum):
    CONTRACT = "contract"
    INVOICE = "invoice"
    INVOICE_VERIFICATION = "invoice_verification"
    CERTIFICATE = "certificate"
    BUSINESS_LICENSE = "business_license"
    TECHNICAL_PARAMETER = "technical_parameter"
    TEST_REPORT = "test_report"
    PERFORMANCE_TABLE = "performance_table"
    PRODUCTION_CAPACITY = "production_capacity"
    PERSONNEL = "personnel"
    AFTER_SALES = "after_sales"
    OTHER = "other"


class ClassificationStatus(StrEnum):
    CLASSIFIED = "classified"
    MANUAL_REVIEW = "manual_review"


class RuleStatus(StrEnum):
    ACTIVE = "active"
    RULE_CONFLICT = "rule_conflict"
    MANUAL_REVIEW = "manual_review"


class RuleCategory(StrEnum):
    FORMAT = "format"
    QUALIFICATION = "qualification"
    COMMERCIAL = "commercial"
    TECHNICAL = "technical"
    PERFORMANCE_CAPACITY = "performance_capacity"
    CONSISTENCY_RISK = "consistency_risk"


class RulePriority(StrEnum):
    CRITICAL = "critical"
    IMPORTANT = "important"
    NORMAL = "normal"


class VerificationLevel(StrEnum):
    BID_ONLY = "bid_only"
    SOURCE_CHECK = "source_check"
    EVIDENCE_CHECK = "evidence_check"
    CROSS_CHECK = "cross_check"


class Severity(StrEnum):
    FATAL = "fatal"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"
    UNKNOWN = "unknown"


class ResponseStatus(StrEnum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    NOT_APPLICABLE = "not_applicable"
    UNREADABLE = "unreadable"


class FindingResult(StrEnum):
    COMPLIANT = "compliant"
    NON_COMPLIANT = "non_compliant"
    RISK = "risk"
    MANUAL_REVIEW = "manual_review"


class EvidenceReviewStatus(StrEnum):
    SUPPORTED = "supported"
    PARTIALLY_SUPPORTED = "partially_supported"
    UNSUPPORTED = "unsupported"
    CONTRADICTED = "contradicted"
    OCR_REQUIRED = "ocr_required"
    UNVERIFIABLE = "unverifiable"
    WEAKLY_RELATED = "weakly_related"


class ProjectMatchStatus(StrEnum):
    MATCHED = "matched"
    NOT_MATCHED = "not_matched"
    UNCERTAIN = "uncertain"


class ProjectStatus(StrEnum):
    PROCESSING_RULES = "processing_rules"
    RULE_READY = "rule_ready"
    RULE_FAILED = "rule_failed"


class BidderStatus(StrEnum):
    PROCESSING_EVIDENCE = "processing_evidence"
    EVIDENCE_READY = "evidence_ready"
    EVIDENCE_FAILED = "evidence_failed"


class OverallConclusion(StrEnum):
    COMPLIANT = "compliant"
    NON_COMPLIANT = "non_compliant"
    RISK = "risk"
    MANUAL_REVIEW = "manual_review"
    NOT_APPLICABLE = "not_applicable"


class TaskStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_REPORT_ERROR = "completed_with_report_error"
    FAILED = "failed"


class ComparisonStatus(StrEnum):
    MATCHED = "matched"
    NOT_MATCHED = "not_matched"
    MISSING = "missing"
    UNSUPPORTED_OPERATOR = "unsupported_operator"
    MANUAL_REVIEW = "manual_review"


class ComparisonResult(StrictModel):
    status: ComparisonStatus
    raw_expected: Any | None = None
    raw_actual: Any | None = None
    normalized_expected: Any | None = None
    normalized_actual: Any | None = None
    message: str | None = None
