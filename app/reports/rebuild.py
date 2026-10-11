"""读取已保存 MD 和运行快照重新导出；不建索引、不调用任何模型。"""

import json
import logging

from app.audit.sources import SourceLedger
from app.graph.state_updates import file_result_from_report
from app.models.results import AuditResult, ReadSource
from app.models.rules import RuleReportItem, present_rule_for_model
from app.parsers.assets import atomic_write
from app.parsers.reading import build_document_units
from app.reports.markdown_report import parse_report
from app.services.audit_task_service import AuditTaskContext

logger = logging.getLogger(__name__)


def rebuild_result(task_id, store):
    directory = store.task_output_dir(task_id)
    context = AuditTaskContext.model_validate_json(
        (directory / "audit_context.json").read_text("utf-8")
    )
    if context.task_id != task_id:
        raise ValueError("审核快照与任务 ID 不符")
    results = {item.file.file_id: item for item in context.routed_file_results}
    errors = dict(context.file_errors)
    for document in context.bid_files:
        key = document.file_id
        try:
            state = json.loads(
                (directory / "drafts" / f"{key}.state.json").read_text("utf-8")
            )
            total = len(build_document_units(context.parsed_documents[key]))
            if (
                state["file_id"] != key
                or state["total_units"] != total
                or state["next_unit_index"] != total
            ):
                raise ValueError("文件仍有未完成的阅读范围，不能只修报告就视为审核完成")
            if state.get("pending_report_saves"):
                raise ValueError("仍有非报告工具结果未保存进度，不能重导出为正式报告")
            if context.report_format_version == 3 and (
                state.get("report_format_version") != 3 or "pending_report_saves" not in state
            ):
                raise ValueError("新版草稿缺少可确认的保存门禁状态，不能推定完成")
            ledger = SourceLedger(context.parsed_documents)
            for source_id, value in state["sources"].items():
                source = ReadSource.model_validate(value)
                parsed = context.parsed_documents[source.file_id]
                if (
                    source.parse_version != parsed.markdown_path
                    or source.data_role != parsed.document.data_role
                ):
                    raise ValueError("读取来源与固定解析版本不一致")
                ledger.sources[source_id] = source
            text = (directory / "drafts" / f"{key}.md").read_text("utf-8")
            decision, _ = parse_report(
                text,
                context.rule_set,
                ledger,
                final=True,
                version=context.report_format_version,
                report_context=context.report_context,
            )
            previous = directory / f"{key}_result.json"
            report_context = context.report_context
            if previous.is_file() and report_context:
                from app.models.results import FileAuditResult

                saved = FileAuditResult.model_validate_json(previous.read_text("utf-8"))
                if saved.report_context and saved.report_context.finalized_at:
                    report_context = report_context.model_copy(
                        update={"finalized_at": saved.report_context.finalized_at}
                    )
            result = file_result_from_report(
                document,
                context.bid_summaries[key],
                decision,
                ledger,
                version=context.report_format_version,
                report_context=report_context,
                rule_order=[r.rule_id for r in context.rule_set.rules],
            )
            results[key] = result
            atomic_write(
                directory / f"{key}_result.json",
                result.model_dump_json(indent=2).encode("utf-8"),
            )
        except Exception as exc:
            logger.exception("报告重导出失败 file_id=%s", key)
            errors[key] = f"{type(exc).__name__}: {exc}"
    return AuditResult(
        task_id=task_id,
        project_id=context.project_id,
        bidder_id=context.bidder_id,
        evidence_library=context.evidence_library,
        rules=[
            RuleReportItem.model_validate(present_rule_for_model(rule).model_dump())
            for rule in context.rule_set.rules
        ],
        rule_source_files=[file.model_dump() for file in context.rule_set.source_files],
        file_results=[results[key] for key in context.file_order if key in results],
        file_errors=errors,
    )
