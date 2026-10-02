"""JSON/Word 输出编排和任务终态更新。"""

import logging

from app.models.results import AuditResult
from app.reports.json_report import write_json_result
from app.reports.presentation import write_formal_reports
from app.reports.word_report import generate_word_reports
from app.services.audit_task_service import OutputFinalizer
from app.storage.file_store import FileStore
from app.storage.repositories import AuditTaskRepository

logger = logging.getLogger(__name__)


class ReportService(OutputFinalizer):
    def __init__(
        self,
        task_repository: AuditTaskRepository | None = None,
        file_store: FileStore | None = None,
    ) -> None:
        self.task_repository = task_repository or AuditTaskRepository()
        self.file_store = file_store or FileStore()

    async def finalize(self, task_id: str, result: AuditResult) -> None:
        """JSON 失败由后台 Runner 标记 failed；Word 失败保留 JSON。"""

        json_path = write_json_result(task_id, result, self.file_store)
        if result.file_errors:
            # 已完成文件仍导出；执行失败不伪装成整批完成或业务人工复核。
            word_paths = []
            report_error = ""
            try:
                write_formal_reports(result, self.file_store)
                word_paths = generate_word_reports(json_path, self.file_store)
            except Exception as exc:
                logger.exception("部分成果 Word 导出失败 task_id=%s", task_id)
                report_error = f"；报告生成失败：{exc}"
                word_paths = [
                    self.file_store.relative_to_data_root(path)
                    for path in self.file_store.task_output_dir(task_id).glob("*.docx")
                ]
            self.task_repository.transition(
                task_id,
                "running",
                "failed",
                json_path=json_path,
                word_paths=word_paths,
                error_code="audit_files_failed",
                error_message="；".join(
                    f"{key}: {value}" for key, value in result.file_errors.items()
                )
                + report_error,
            )
            return
        try:
            write_formal_reports(result, self.file_store)
            word_paths = generate_word_reports(json_path, self.file_store)
            self.task_repository.mark_completed(
                task_id,
                json_path=json_path,
                word_paths=word_paths,
            )
        except Exception as exc:
            logger.exception("Word 导出失败 task_id=%s", task_id)
            existing = [
                self.file_store.relative_to_data_root(path)
                for path in self.file_store.task_output_dir(task_id).glob("*.docx")
            ]
            self.task_repository.mark_completed_with_report_error(
                task_id,
                json_path=json_path,
                word_paths=existing,
                error_message=str(exc),
            )

    def rerender_word_reports(self, task_id: str) -> list[str]:
        """只读取已有 JSON 重新生成 Word，绝不重新运行审核 Agent。"""

        task = self.task_repository.require(task_id)
        if task.status != "completed_with_report_error" or not task.json_path:
            raise ValueError("任务没有可用于 Word 重渲染的已校验 JSON")
        from app.reports.json_report import read_json_result

        write_formal_reports(
            read_json_result(task.json_path, self.file_store), self.file_store
        )
        word_paths = generate_word_reports(task.json_path, self.file_store)
        self.task_repository.recover_report_outputs(
            task_id,
            json_path=task.json_path,
            word_paths=word_paths,
        )
        return word_paths

    def rebuild_from_markdown(self, task_id: str) -> list[str]:
        """显式修复 MD 后重导出，不重跑 Agent；未读完的文件仍保持失败。"""
        from app.reports.rebuild import rebuild_result

        task = self.task_repository.require(task_id)
        if task.status == "running":
            raise ValueError("运行中的任务不能并行修改报告")
        result = rebuild_result(task_id, self.file_store)
        json_path = write_json_result(task_id, result, self.file_store)
        error = "；".join(
            f"{key}: {value}" for key, value in result.file_errors.items()
        )
        status = "failed" if result.file_errors else "completed"
        code = "audit_files_failed" if result.file_errors else ""
        try:
            write_formal_reports(result, self.file_store)
            paths = generate_word_reports(json_path, self.file_store)
        except Exception as exc:
            logger.exception("修复报告后 Word 导出失败 task_id=%s", task_id)
            paths = [
                self.file_store.relative_to_data_root(path)
                for path in self.file_store.task_output_dir(task_id).glob("*.docx")
            ]
            error += f"；Word 导出失败：{exc}"
            if not result.file_errors:
                status, code = "completed_with_report_error", "word_render_error"
        self.task_repository.transition(
            task_id,
            task.status,
            status,
            json_path=json_path,
            word_paths=paths,
            error_code=code,
            error_message=error,
        )
        return paths
