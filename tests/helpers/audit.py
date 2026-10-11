"""Task24 测试样本：真实新契约，假模型不绕过工具/控制器。"""

import re
from types import SimpleNamespace

from langchain_core.messages import AIMessage

from app.models.documents import (
    BidDocumentSummary,
    DocumentRef,
    ParsedBlock,
    ParsedDocument,
    ParsedSection,
)
from app.models.evidence import EvidenceLibrary, EvidenceMaterial, EvidenceSourceFile
from app.models.rules import RuleItem, RuleSet, RuleSourceFile
from app.prompts.loader import render_prompt
from app.reports.markdown_report import TITLE_PLACEHOLDER, skeleton
from app.services.audit_task_service import AuditTaskContext


def parsed_file(file_id, role="bid_document", text=None):
    document = DocumentRef(
        file_id=file_id,
        file_name=file_id + ".md",
        source_path=file_id + ".md",
        data_role=role,
        parse_status="parsed",
        project_id=None if role == "evidence_material" else "project-1",
        bidder_id=None if role == "rule_source" else "bidder-1",
        task_id="audit-1" if role == "bid_document" else None,
    )
    return ParsedDocument(
        schema_version=2,
        document=document,
        parse_status="parsed",
        parser_version="test",
        markdown_path=file_id + "/versions/v1/document.md",
        sections=[
            ParsedSection(
                section_id="s1",
                title="投标函",
                blocks=[
                    ParsedBlock(
                        kind="text",
                        source_block_index=0,
                        content=text or f"{file_id}独立正文：分辨率640×480。",
                    )
                ],
            )
        ],
    )


def audit_context(count=1):
    source = RuleSourceFile(
        file_id="tender-1", file_name="tender-1.md", source_path="tender-1.md"
    )
    rules = RuleSet(
        project_id="project-1",
        project_profile="红外热成像仪采购",
        source_files=[source],
        rules=[
            RuleItem(
                rule_id="rule-1",
                provenance="ai_generated",
                category="technical",
                requirement="分辨率至少640×480",
                priority="critical",
                verification="evidence_check",
                reason="依据招标技术要求",
                sources=[source],
            )
        ],
    )
    library = EvidenceLibrary(
        bidder_id="bidder-1",
        source_files=[
            EvidenceSourceFile(
                file_id=f"e-{i}", file_name=f"e-{i}.md", source_path=f"e-{i}.md"
            )
            for i in (1, 2, 3)
        ],
        materials=[
            EvidenceMaterial(
                file_id=f"e-{i}",
                bid_summary=f"资料{i}简洁目录",
                evidence_summary=f"资料{i}详细事实：设备型号ABC，分辨率640×480。",
                relations=[] if i == 1 else [("支持", f"e-{i - 1}")],
            )
            for i in (1, 2, 3)
        ],
    )
    parsed = [
        parsed_file("tender-1", "rule_source"),
        *[parsed_file(f"e-{i}", "evidence_material") for i in (1, 2, 3)],
        *[parsed_file(f"bid-{i}") for i in range(1, count + 1)],
    ]
    bids = [p.document for p in parsed if p.document.data_role == "bid_document"]
    return AuditTaskContext(
        task_id="audit-1",
        project_id="project-1",
        bidder_id="bidder-1",
        rule_set=rules,
        evidence_library=library,
        bid_files=bids,
        file_order=[f.file_id for f in bids],
        parsed_documents={p.document.file_id: p for p in parsed},
        report_template=render_prompt("audit_report_template"),
        bid_summaries={
            f.file_id: BidDocumentSummary(
                file_id=f.file_id, match_status="matched", match_reason="对象一致"
            )
            for f in bids
        },
    )


def filled_report(
    rule_set,
    reason="分辨率满足要求",
    conclusion="符合",
    title="设备采购投标文件审核报告",
    version=1,
):
    text = skeleton(rule_set, version).replace(
        f"# {TITLE_PLACEHOLDER}", f"# {title}", 1
    )
    if version in (2, 3):
        text = text.replace("## 重点问题\n\n待填写。", "## 重点问题\n\n无重点问题。")
    text = text.replace(
        "## 总体总结\n\n待填写。", "## 总体总结\n\n文件审核完成，详见分类结论。"
    )
    text = text.replace(
        "### 分类总结\n\n待填写。", "### 分类总结\n\n当前类别要求已检查。"
    )
    return re.sub(
        r"(### rule-[^\n]+\n\n)待填写。",
        lambda m: (
            m[1]
            + ("核验状态：已完成\n进度说明：已核对必要事实。\n" if version == 3 else "")
            + f"结论：{conclusion}\n"
            + ("实际发现：技术响应表载明参数。\n" if version in (2, 3) else "")
            + f"审核说明：{reason}"
        ),
        text,
    )


def candidate_response(requirement, explanation="本批原文明示的要求。"):
    """脚本模型同轮编辑候选并finish，仍经过真实阶段和文本校验。"""
    from app.rules.context import CANDIDATE_TEMPLATE

    draft = (
        "# 当前文件候选草稿\n\n候选简介：" + requirement
        + "\n\n## 候选清单\n\n### 候选 1\n\n要求：" + requirement
        + "\n\n候选说明：" + explanation
    )
    return AIMessage(content="", tool_calls=[
        {"name": "rule_candidates", "id": "candidate-edit", "args": {
            "action": "edit", "edits": [{"old_text": CANDIDATE_TEMPLATE, "new_text": draft}]}},
        {"name": "rule_candidates", "id": "candidate-finish", "args": {"action": "finish"}},
    ])


class SequenceModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.bindings = []

    def bind_tools(self, tools):
        self.bindings.append([tool.name for tool in tools])
        return self

    async def ainvoke(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return (
            response if isinstance(response, AIMessage) else AIMessage(content=response)
        )


def tool_response(name, args, call_id="call-1"):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def model_bundle(bid_model):
    return SimpleNamespace(
        bid_chat=bid_model,
        evidence_chat=SequenceModel([]),
        embedding=object(),
        rerank=object(),
    )
