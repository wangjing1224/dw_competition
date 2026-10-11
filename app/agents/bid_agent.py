"""主审核消息装配：完整规则与独立模板只在 System，当前原文在 Human。"""

from langchain_core.messages import HumanMessage, SystemMessage

from app.audit.context import brief_catalog
from app.audit.session import audit_agent_node as bid_agent_node
from app.parsers.reading import render_document_range
from app.prompts.loader import render_prompt
from app.rules.context import build_rule_view

__all__ = ["bid_agent_node", "build_bid_agent_messages"]


def build_bid_agent_messages(context, parsed, units, start, end, report, handoff=""):
    document = parsed.document
    return [
        SystemMessage(
            content=render_prompt(
                "bid_agent",
                rule_view=build_rule_view(context.rule_set, include_profile=False),
                report_markdown_contract=context.report_template,
            )
        ),
        HumanMessage(
            content=render_prompt(
                "bid_agent_input",
                file_identity=f"文件ID：{document.file_id}\n文件名：{document.file_name}\n所选项目：{context.project_id}\n资料库：{context.bidder_id}",
                project_profile=context.rule_set.project_profile,
                reading_scope=f"完整结构第{start + 1}至{end}，共{len(units)}个；前{start}个已处理，"
                + ("还有后部未读。" if end < len(units) else "本批包含当前解析结构末尾，不代表已确认真实提交包完整。")
                + "图片入口不代表已查看。",
                document_context=render_document_range(parsed, units, start, end),
                evidence_catalog=brief_catalog(context.evidence_library),
                report_content=report.read(),
                continuation=handoff
                or (
                    "继续同一文件，保留已保存且仍成立的结果。"
                    if start
                    else "首个阅读批次。"
                ),
            )
        ),
    ]
