"""JSON/Word 输出编排和任务终态更新。"""

from app.models.results import AuditResult
from app.reports.json_report import AuditResultOutputError, write_json_result
from app.reports.word_report import generate_word_reports
from app.services.audit_task_service import OutputFinalizer
from app.storage.file_store import FileStore
from app.storage.repositories import AuditTaskRepository


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
        try:
            word_paths = generate_word_reports(json_path, self.file_store)
            self.task_repository.mark_completed(
                task_id,
                json_path=json_path,
                word_paths=word_paths,
            )
        except Exception as exc:
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
        word_paths = generate_word_reports(task.json_path, self.file_store)
        self.task_repository.recover_report_outputs(
            task_id,
            json_path=task.json_path,
            word_paths=word_paths,
        )
        return word_paths
