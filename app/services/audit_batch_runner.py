"""完整单文件流水线：有限并发、固定依据、独立失败、结果就绪立即发布。"""

import asyncio
import logging

from app.audit.bid_summary import summarize_and_route_bid_document
from app.audit.concurrency import file_limiter, model_limiter
from app.audit.local_trace import AuditTrace, bind, safe_value, scope
from app.core.config import get_settings
from app.models.documents import ParsedDocument, document_ref_from_record
from app.models.evidence import EvidenceLibrary
from app.models.results import (
    AuditBatchFile,
    AuditBatchResult,
    AuditResult,
    ReportContext,
)
from app.models.rules import RuleReportItem, RuleSet, present_rule_for_model
from app.parsers.assets import atomic_write
from app.parsers.service import prepare_document
from app.reports.presentation import write_formal_markdown
from app.reports.word_report import generate_word_reports
from app.retrieval.index import build_document_index
from app.retrieval.models import IndexScope
from app.services.audit_management_service import AuditManagementService
from app.services.audit_task_service import AuditTaskContext, _routed_file_result
from app.storage.audit_management import (
    aggregate_status,
    file_row,
    file_rows,
    update_file,
    update_management,
)
from app.storage.database import open_db
from app.storage.repositories import utc_now

logger = logging.getLogger(__name__)


def error_view(exc):
    return safe_value(
        {"code": getattr(exc, "code", type(exc).__name__), "message": str(exc)}
    )[0]


def fail_file(task_id, file_id, exc):
    p = file_row(task_id, file_id)["payload"]
    phase = p["phase"]
    if p.get("json_path"):
        # 业务结果已发布后中断，只影响导出，不撤销成功的正式结论。
        update_file(
            task_id,
            file_id,
            stage="exporting",
            stage_status="failed",
            status="completed",
            word_status="failed",
            export_error=error_view(exc),
            completed_at=utc_now(),
        )
        return
    update_file(
        task_id,
        file_id,
        stage=phase if phase in p["stages"] else None,
        stage_status="failed",
        status="failed",
        failed_phase=phase,
        error=error_view(exc),
        completed_at=utc_now(),
    )


async def run_audit_batch(task_id, *, models=None, executor=None):
    service = AuditManagementService()
    settings = get_settings()
    try:
        frozen = service.snapshot(task_id)
        if models is None:
            from app.llm.factory import build_audit_models

            models = build_audit_models(settings)
        if executor is None:
            from app.graph.audit_task_graph import LangGraphAuditExecutor

            executor = LangGraphAuditExecutor(settings, models)

        async def run_file(row):
            file_id = row["file_id"]
            trace = AuditTrace(task_id, file_id)
            # 同进程所有批次共用容量，不为每批复制一组无限执行名额。
            async with file_limiter(settings.audit.file_concurrency):
                try:
                    with (
                        bind(trace),
                        scope("file", service.files.require(file_id).file_name),
                    ):
                        await process_file(
                            service, settings, models, executor, frozen, row
                        )
                except Exception as exc:
                    logger.exception("逐文件审核失败 task=%s file=%s", task_id, file_id)
                    fail_file(task_id, file_id, exc)

        await asyncio.gather(*(run_file(row) for row in file_rows(task_id)))
    except BaseException as exc:
        logger.exception("审核批次执行异常 task=%s", task_id)
        for row in file_rows(task_id):
            if row["payload"]["status"] in {"queued", "running"}:
                fail_file(task_id, row["file_id"], exc)
        if isinstance(exc, asyncio.CancelledError):
            raise
    finally:
        finish_batch(service, task_id)


async def process_file(service, settings, models, executor, frozen, row):
    task_id, file_id = row["task_id"], row["file_id"]
    directory = service.store.task_output_dir(task_id)
    rules = RuleSet.model_validate(frozen["projects"][row["project_id"]]["rule_set"])
    library = EvidenceLibrary.model_validate(
        frozen["bidders"][row["bidder_id"]]["library"]
    )
    source_ids = {f.file_id for f in rules.source_files} | {
        f.file_id for f in library.source_files
    }
    context = AuditTaskContext(
        task_id=task_id,
        project_id=row["project_id"],
        bidder_id=row["bidder_id"],
        rule_set=rules,
        evidence_library=library,
        report_template=frozen["report_template"],
        report_format_version=frozen.get("report_format_version", 1),
        report_context=ReportContext(
            task_name=frozen.get("task_name"),
            rule_library_name=frozen["projects"][row["project_id"]]["name"],
            evidence_library_name=frozen["bidders"][row["bidder_id"]]["name"],
        )
        if frozen.get("report_format_version") == 2
        else None,
        file_order=[file_id],
        parsed_documents={
            key: ParsedDocument.model_validate(frozen["parsed"][key])
            for key in source_ids
        },
        index_scope_overrides={file_id: f"bid-{task_id}-{file_id}"},
    )
    document = document_ref_from_record(service.files.require(file_id))

    update_file(
        task_id, file_id, status="running", stage="parsing", stage_status="running"
    )
    with scope("parsing", "文档解析"):
        parsed = await prepare_document(document)
        parsed_path = directory / "parsed" / f"{file_id}.json"
        atomic_write(parsed_path, parsed.model_dump_json().encode())
        update_file(
            task_id,
            file_id,
            stage="parsing",
            stage_status="completed",
            parsed_path=service.store.relative_to_data_root(parsed_path),
        )
    context.parsed_documents[file_id] = parsed

    update_file(task_id, file_id, stage="routing", stage_status="running")
    with scope(
        "routing",
        "项目适用性判断",
        actor="router",
        input={"file_id": file_id, "project_id": row["project_id"]},
    ) as observation:
        route = await summarize_and_route_bid_document(
            parsed,
            rules,
            models.bid_summary,
            settings.bid_summary_model,
            limiter=model_limiter(settings.audit.model_concurrency),
            trace_context={"metadata": {"task_id": task_id, "file_id": file_id}},
        )
        observation["output"] = route.model_dump(mode="json")
        context.bid_summaries[file_id] = route
        update_file(
            task_id,
            file_id,
            stage="routing",
            stage_status="completed",
            route=route.model_dump(mode="json", exclude={"file_id"}),
        )
    context_path = directory / "contexts" / f"{file_id}.json"
    atomic_write(context_path, context.model_dump_json().encode())
    if route.match_status == "matched":
        update_file(task_id, file_id, stage="indexing", stage_status="running")
        with scope("indexing", "建立投标原文索引"):
            await build_document_index(
                IndexScope(
                    scope_id=context.index_scope_overrides[file_id],
                    data_role="bid_document",
                    project_id=row["project_id"],
                    bidder_id=row["bidder_id"],
                    task_id=task_id,
                ),
                [document],
                models.embedding,
                settings,
            )
        update_file(task_id, file_id, stage="indexing", stage_status="completed")
        context.bid_files = [document]
        atomic_write(context_path, context.model_dump_json().encode())
        update_file(task_id, file_id, stage="auditing", stage_status="running")
        with scope("auditing", "主 Agent 审核", actor="bid_agent"):
            result = await executor.run(context)
            if result.file_errors:
                raise RuntimeError(result.file_errors.get(file_id, "审核执行失败"))
            # 即使测试替身或外部执行器返回，也必须通过同一正式覆盖和来源校验。
            result = AuditResult.model_validate(result.model_dump())
            if (
                len(result.file_results) != 1
                or result.file_results[0].file.file_id != file_id
            ):
                raise ValueError("执行器返回了其他文件的结果")
            if (result.task_id, result.project_id, result.bidder_id) != (
                task_id,
                row["project_id"],
                row["bidder_id"],
            ) or result.evidence_library != library:
                raise ValueError("执行器结果与冻结审核依据不一致")
            expected_rules = [
                RuleReportItem.model_validate(present_rule_for_model(r).model_dump())
                for r in rules.rules
            ]
            if result.rules != expected_rules:
                raise ValueError("执行器返回的规则不是本次冻结版本")
            if (
                result.file_results[0].report_format_version
                != context.report_format_version
            ):
                raise ValueError("执行器返回的报告格式版本与受理快照不一致")
        update_file(task_id, file_id, stage="auditing", stage_status="completed")
    else:
        for phase in ("indexing", "auditing"):
            update_file(
                task_id,
                file_id,
                stage=phase,
                stage_status="skipped",
                reason=route.match_reason,
            )
        result = AuditResult(
            task_id=task_id,
            project_id=row["project_id"],
            bidder_id=row["bidder_id"],
            rules=[
                RuleReportItem.model_validate(present_rule_for_model(r).model_dump())
                for r in rules.rules
            ],
            evidence_library=library,
            rule_source_files=[f.model_dump() for f in rules.source_files],
            file_results=[_routed_file_result(document, route)],
        )
        atomic_write(
            directory / "drafts" / f"{file_id}.md",
            f"# {result.file_results[0].report_title}\n\n{route.match_reason}\n".encode(),
        )

    file_result = result.file_results[0]
    if context.report_format_version == 2:
        file_result.report_format_version = 2
        finalized_at = (
            file_result.report_context.finalized_at
            if file_result.report_context
            else None
        ) or utc_now()
        file_result.report_context = ReportContext.model_validate(
            {
                **context.report_context.model_dump(),
                "finalized_at": finalized_at,
            }
        )
        # 按完整冻结顺序做兼容投影，不根据模型输出顺序改变建议顺序。
        by_rule = {f.rule_id: f for f in file_result.findings}
        file_result.suggestions = list(
            dict.fromkeys(
                by_rule[r.rule_id].recommendation.strip()
                for r in result.rules
                if r.rule_id in by_rule
                and by_rule[r.rule_id].recommendation
                and by_rule[r.rule_id].recommendation.strip()
            )
        )
        result = AuditResult.model_validate(result.model_dump())
    json_path = directory / "reports" / f"{file_id}.json"
    md_path = directory / "drafts" / f"{file_id}.md"
    if not md_path.is_file():
        raise RuntimeError("正式结果缺少已保存的 Markdown 报告")
    atomic_write(json_path, result.model_dump_json(indent=2).encode())
    update_file(
        task_id,
        file_id,
        stage="exporting",
        stage_status="running",
        json_path=service.store.relative_to_data_root(json_path),
        md_path=service.store.relative_to_data_root(md_path)
        if context.report_format_version == 1
        else None,
        report_kind="final",
        overall_conclusion=str(result.file_results[0].overall_conclusion),
        word_status="unavailable",
    )
    if context.report_format_version == 2:
        try:
            md_path = write_formal_markdown(
                result, result.file_results[0], service.store
            )
            update_file(
                task_id, file_id, md_path=service.store.relative_to_data_root(md_path)
            )
        except Exception:
            logger.exception("正式 Markdown 导出失败 task=%s file=%s", task_id, file_id)
            update_file(
                task_id,
                file_id,
                stage="exporting",
                stage_status="failed",
                export_error={
                    "code": "markdown_export_failed",
                    "message": "正式 Markdown 导出失败，已保留有效 JSON",
                },
                status="completed",
                phase="finished",
                completed_at=utc_now(),
            )
            return
    update_file(task_id, file_id, word_status="generating")
    try:
        with scope("export", "Word 导出"):
            paths = await asyncio.to_thread(
                generate_word_reports,
                service.store.relative_to_data_root(json_path),
                service.store,
            )
            if len(paths) != 1:
                raise RuntimeError("单文件报告导出数量不符")
        update_file(
            task_id,
            file_id,
            stage="exporting",
            stage_status="completed",
            word_status="ready",
            word_path=paths[0],
        )
    except Exception as exc:
        logger.exception("Word 导出失败 task=%s file=%s", task_id, file_id)
        update_file(
            task_id,
            file_id,
            stage="exporting",
            stage_status="failed",
            word_status="failed",
            export_error={**error_view(exc), "code": "word_export_failed"},
        )
    update_file(
        task_id, file_id, status="completed", phase="finished", completed_at=utc_now()
    )


def finish_batch(service, task_id):
    rows = file_rows(task_id)
    status = aggregate_status([r["payload"] for r in rows])
    json_path, error = None, None
    try:
        frozen = service.snapshot(task_id)
        entries = []
        for row in rows:
            result = None
            if row["payload"].get("json_path"):
                result = AuditResult.model_validate_json(
                    service.store.resolve_relative(
                        row["payload"]["json_path"]
                    ).read_bytes()
                ).file_results[0]
            entries.append(
                AuditBatchFile(
                    file_id=row["file_id"],
                    project_id=row["project_id"],
                    bidder_id=row["bidder_id"],
                    status=row["payload"]["status"],
                    result=result,
                    error=row["payload"].get("error"),
                    export_error=row["payload"].get("export_error"),
                )
            )
        output = AuditBatchResult(
            task_id=task_id,
            rule_libraries={
                k: RuleSet.model_validate(v["rule_set"])
                for k, v in frozen["projects"].items()
            },
            evidence_libraries={
                k: EvidenceLibrary.model_validate(v["library"])
                for k, v in frozen["bidders"].items()
            },
            files=entries,
        )
        path = service.store.task_output_dir(task_id) / "audit_batch.json"
        atomic_write(path, output.model_dump_json(indent=2).encode())
        json_path = service.store.relative_to_data_root(path)
    except Exception as exc:
        logger.exception("批次汇总失败 task=%s", task_id)
        status, error = "failed", error_view(exc)
    with open_db() as db:
        db.execute(
            "UPDATE audit_tasks SET status=?,json_path=?,error_code=?,error_message=?,updated_at=? WHERE task_id=?",
            (
                status,
                json_path,
                error["code"] if error else None,
                error["message"] if error else None,
                utc_now(),
                task_id,
            ),
        )
    update_management(task_id, completed_at=utc_now())
