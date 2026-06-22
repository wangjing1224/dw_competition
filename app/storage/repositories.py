"""项目、投标方、文件和审核任务元数据 Repository。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.errors import NotFoundError, StateConflict
from app.models.common import generate_id
from app.storage.database import open_db


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class ProjectRecord:
    project_id: str
    status: str
    rule_set_path: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class BidderRecord:
    project_id: str
    bidder_id: str
    status: str
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class AuditTaskRecord:
    task_id: str
    project_id: str
    bidder_id: str
    status: str
    progress: dict[str, Any]
    error_code: str | None = None
    error_message: str | None = None
    json_path: str | None = None
    word_paths: list[str] | None = None


@dataclass(frozen=True)
class AuditEventRecord:
    """供任务观测页展示的可读事件，不保存模型隐藏思维过程。"""

    event_id: int
    task_id: str
    actor: str
    event_type: str
    title: str
    detail: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class FileRecord:
    file_id: str
    project_id: str
    bidder_id: str | None
    task_id: str | None
    data_role: str
    file_name: str
    source_path: str
    storage_path: str
    extension: str
    size_bytes: int
    sha256: str
    parse_status: str
    evidence_type: str | None = None
    classification_confidence: float | None = None
    classification_status: str | None = None


class ProjectRepository:
    def create(self, status: str = "processing_rules") -> ProjectRecord:
        project_id = generate_id("project")
        now = utc_now()
        with open_db() as connection:
            connection.execute(
                """
                INSERT INTO projects(project_id, status, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (project_id, status, now, now),
            )
        return self.require(project_id)

    def require(self, project_id: str) -> ProjectRecord:
        with open_db() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("project_not_found", "招标项目不存在")
        return ProjectRecord(
            project_id=row["project_id"],
            status=row["status"],
            rule_set_path=row["rule_set_path"],
            error_code=row["error_code"],
            error_message=row["error_message"],
        )

    def update_status(
        self,
        project_id: str,
        status: str,
        *,
        rule_set_path: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> ProjectRecord:
        with open_db() as connection:
            updated = connection.execute(
                """
                UPDATE projects
                   SET status = ?, rule_set_path = ?, error_code = ?,
                       error_message = ?, updated_at = ?
                 WHERE project_id = ?
                """,
                (
                    status,
                    rule_set_path,
                    error_code,
                    error_message,
                    utc_now(),
                    project_id,
                ),
            ).rowcount
        if updated != 1:
            raise NotFoundError("project_not_found", "招标项目不存在")
        return self.require(project_id)


class BidderRepository:
    def create(
        self, project_id: str, status: str = "processing_evidence"
    ) -> BidderRecord:
        ProjectRepository().require(project_id)
        bidder_id = generate_id("bidder")
        now = utc_now()
        with open_db() as connection:
            connection.execute(
                """
                INSERT INTO bidders(
                    project_id, bidder_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (project_id, bidder_id, status, now, now),
            )
        return self.require(project_id, bidder_id)

    def require(self, project_id: str, bidder_id: str) -> BidderRecord:
        with open_db() as connection:
            row = connection.execute(
                """
                SELECT * FROM bidders
                 WHERE project_id = ? AND bidder_id = ?
                """,
                (project_id, bidder_id),
            ).fetchone()
        if row is None:
            raise NotFoundError("bidder_not_found", "投标方不存在或不属于当前项目")
        return BidderRecord(
            project_id=row["project_id"],
            bidder_id=row["bidder_id"],
            status=row["status"],
            error_code=row["error_code"],
            error_message=row["error_message"],
        )

    def update_status(
        self,
        project_id: str,
        bidder_id: str,
        status: str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> BidderRecord:
        """更新投标方资料处理状态，归属条件始终包含 project_id。"""

        with open_db() as connection:
            updated = connection.execute(
                """
                UPDATE bidders
                   SET status = ?, error_code = ?, error_message = ?, updated_at = ?
                 WHERE project_id = ? AND bidder_id = ?
                """,
                (
                    status,
                    error_code,
                    error_message,
                    utc_now(),
                    project_id,
                    bidder_id,
                ),
            ).rowcount
        if updated != 1:
            raise NotFoundError("bidder_not_found", "投标方不存在或不属于当前项目")
        return self.require(project_id, bidder_id)


class AuditTaskRepository:
    def create(
        self,
        project_id: str,
        bidder_id: str,
        *,
        status: str = "running",
        progress: dict[str, Any] | None = None,
    ) -> AuditTaskRecord:
        BidderRepository().require(project_id, bidder_id)
        task_id = generate_id("audit")
        now = utc_now()
        with open_db() as connection:
            connection.execute(
                """
                INSERT INTO audit_tasks(
                    task_id, project_id, bidder_id, status, progress_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    project_id,
                    bidder_id,
                    status,
                    json.dumps(progress or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return self.require(task_id)

    def require(self, task_id: str) -> AuditTaskRecord:
        with open_db() as connection:
            row = connection.execute(
                "SELECT * FROM audit_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("audit_task_not_found", "审核任务不存在")
        return AuditTaskRecord(
            task_id=row["task_id"],
            project_id=row["project_id"],
            bidder_id=row["bidder_id"],
            status=row["status"],
            progress=json.loads(row["progress_json"]),
            error_code=row["error_code"],
            error_message=row["error_message"],
            json_path=row["json_path"],
            word_paths=json.loads(row["word_paths_json"]),
        )

    def transition(
        self,
        task_id: str,
        expected_status: str,
        new_status: str,
        **changes: Any,
    ) -> AuditTaskRecord:
        progress = changes.get("progress")
        with open_db() as connection:
            updated = connection.execute(
                """
                UPDATE audit_tasks
                   SET status = ?,
                       progress_json = COALESCE(?, progress_json),
                       error_code = COALESCE(?, error_code),
                       error_message = COALESCE(?, error_message),
                       json_path = COALESCE(?, json_path),
                       word_paths_json = COALESCE(?, word_paths_json),
                       updated_at = ?
                 WHERE task_id = ? AND status = ?
                """,
                (
                    new_status,
                    json.dumps(progress, ensure_ascii=False)
                    if progress is not None
                    else None,
                    changes.get("error_code"),
                    changes.get("error_message"),
                    changes.get("json_path"),
                    json.dumps(changes["word_paths"], ensure_ascii=False)
                    if "word_paths" in changes
                    else None,
                    utc_now(),
                    task_id,
                    expected_status,
                ),
            ).rowcount
        if updated != 1:
            raise StateConflict(
                "审核任务状态已变化",
                {"task_id": task_id, "expected_status": expected_status},
            )
        return self.require(task_id)

    def update_progress(
        self, task_id: str, progress: dict[str, Any]
    ) -> AuditTaskRecord:
        """只更新展示进度，不以 progress 字段判断任务是否完成。"""

        with open_db() as connection:
            updated = connection.execute(
                """
                UPDATE audit_tasks
                   SET progress_json = ?, updated_at = ?
                 WHERE task_id = ? AND status = 'running'
                """,
                (json.dumps(progress, ensure_ascii=False), utc_now(), task_id),
            ).rowcount
        if updated != 1:
            raise StateConflict("审核任务已不处于 running 状态")
        return self.require(task_id)

    def mark_failed(
        self,
        task_id: str,
        *,
        error_code: str,
        error_message: str,
    ) -> AuditTaskRecord:
        return self.transition(
            task_id,
            "running",
            "failed",
            error_code=error_code,
            error_message=error_message,
        )

    def mark_completed(
        self,
        task_id: str,
        *,
        json_path: str,
        word_paths: list[str],
    ) -> AuditTaskRecord:
        return self.transition(
            task_id,
            "running",
            "completed",
            json_path=json_path,
            word_paths=word_paths,
        )

    def mark_completed_with_report_error(
        self,
        task_id: str,
        *,
        json_path: str,
        word_paths: list[str],
        error_message: str,
    ) -> AuditTaskRecord:
        return self.transition(
            task_id,
            "running",
            "completed_with_report_error",
            json_path=json_path,
            word_paths=word_paths,
            error_code="word_render_error",
            error_message=error_message,
        )

    def recover_report_outputs(
        self,
        task_id: str,
        *,
        json_path: str,
        word_paths: list[str],
    ) -> AuditTaskRecord:
        """Word 独立重渲染成功后，从报告错误状态恢复为 completed。"""

        with open_db() as connection:
            updated = connection.execute(
                """
                UPDATE audit_tasks
                   SET status = 'completed', json_path = ?,
                       word_paths_json = ?, error_code = NULL,
                       error_message = NULL, updated_at = ?
                 WHERE task_id = ? AND status = 'completed_with_report_error'
                """,
                (
                    json_path,
                    json.dumps(word_paths, ensure_ascii=False),
                    utc_now(),
                    task_id,
                ),
            ).rowcount
        if updated != 1:
            raise StateConflict("任务不处于可重渲染报告的状态")
        return self.require(task_id)

    def mark_interrupted_tasks_failed(self) -> int:
        with open_db() as connection:
            updated = connection.execute(
                """
                UPDATE audit_tasks
                   SET status = 'failed',
                       error_code = 'process_interrupted',
                       error_message = '应用重启导致进程内任务中断',
                       updated_at = ?
                 WHERE status = 'running'
                """,
                (utc_now(),),
            ).rowcount
        return updated


class AuditEventRepository:
    """按任务追加和读取 Agent、Tool、Pipeline 的轻量观测事件。"""

    def append(
        self,
        task_id: str,
        *,
        actor: str,
        event_type: str,
        title: str,
        detail: dict[str, Any] | None = None,
    ) -> AuditEventRecord:
        now = utc_now()
        with open_db() as connection:
            cursor = connection.execute(
                """
                INSERT INTO audit_events(
                    task_id, actor, event_type, title, detail_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    actor,
                    event_type,
                    title,
                    json.dumps(detail or {}, ensure_ascii=False),
                    now,
                ),
            )
            event_id = int(cursor.lastrowid)
        return AuditEventRecord(
            event_id=event_id,
            task_id=task_id,
            actor=actor,
            event_type=event_type,
            title=title,
            detail=detail or {},
            created_at=now,
        )

    def list_by_task(
        self,
        task_id: str,
        *,
        limit: int = 500,
    ) -> list[AuditEventRecord]:
        AuditTaskRepository().require(task_id)
        with open_db() as connection:
            rows = connection.execute(
                """
                SELECT * FROM audit_events
                 WHERE task_id = ?
                 ORDER BY event_id ASC
                 LIMIT ?
                """,
                (task_id, limit),
            ).fetchall()
        return [
            AuditEventRecord(
                event_id=row["event_id"],
                task_id=row["task_id"],
                actor=row["actor"],
                event_type=row["event_type"],
                title=row["title"],
                detail=json.loads(row["detail_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]


class FileRepository:
    def create(self, record: FileRecord) -> FileRecord:
        now = utc_now()
        values = asdict(record)
        with open_db() as connection:
            connection.execute(
                """
                INSERT INTO files(
                    file_id, project_id, bidder_id, task_id, data_role,
                    file_name, source_path, storage_path, extension,
                    size_bytes, sha256, parse_status, evidence_type,
                    classification_confidence, classification_status,
                    created_at, updated_at
                ) VALUES (
                    :file_id, :project_id, :bidder_id, :task_id, :data_role,
                    :file_name, :source_path, :storage_path, :extension,
                    :size_bytes, :sha256, :parse_status, :evidence_type,
                    :classification_confidence, :classification_status,
                    :created_at, :updated_at
                )
                """,
                {**values, "created_at": now, "updated_at": now},
            )
        return record

    def require(self, file_id: str) -> FileRecord:
        with open_db() as connection:
            row = connection.execute(
                "SELECT * FROM files WHERE file_id = ?", (file_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("file_not_found", "文件不存在")
        fields = {field: row[field] for field in FileRecord.__dataclass_fields__}
        return FileRecord(**fields)

    def update_parse_status(self, file_id: str, parse_status: str) -> FileRecord:
        with open_db() as connection:
            updated = connection.execute(
                """
                UPDATE files
                   SET parse_status = ?, updated_at = ?
                 WHERE file_id = ?
                """,
                (parse_status, utc_now(), file_id),
            ).rowcount
        if updated != 1:
            raise NotFoundError("file_not_found", "文件不存在")
        return self.require(file_id)

    def update_classification(
        self,
        file_id: str,
        *,
        evidence_type: str,
        confidence: float,
        classification_status: str,
    ) -> FileRecord:
        """保存资料分类结果，使后续重建索引无需再次调用分类模型。"""

        with open_db() as connection:
            updated = connection.execute(
                """
                UPDATE files
                   SET evidence_type = ?, classification_confidence = ?,
                       classification_status = ?, updated_at = ?
                 WHERE file_id = ? AND data_role = 'evidence_material'
                """,
                (
                    evidence_type,
                    confidence,
                    classification_status,
                    utc_now(),
                    file_id,
                ),
            ).rowcount
        if updated != 1:
            raise NotFoundError("evidence_file_not_found", "资料文件不存在")
        return self.require(file_id)

    def list_by_scope(
        self,
        *,
        project_id: str,
        data_role: str,
        bidder_id: str | None = None,
        task_id: str | None = None,
    ) -> list[FileRecord]:
        clauses = ["project_id = ?", "data_role = ?"]
        values: list[Any] = [project_id, data_role]
        if bidder_id is not None:
            clauses.append("bidder_id = ?")
            values.append(bidder_id)
        if task_id is not None:
            clauses.append("task_id = ?")
            values.append(task_id)
        with open_db() as connection:
            rows = connection.execute(
                f"SELECT * FROM files WHERE {' AND '.join(clauses)}",
                values,
            ).fetchall()
        return [
            FileRecord(
                **{
                    field: row[field]
                    for field in FileRecord.__dataclass_fields__
                }
            )
            for row in rows
        ]
