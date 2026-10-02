"""审核管理投影。状态变更短事务串行，文件执行上下文不共享。"""

import json

from app.core.errors import NotFoundError
from app.storage.database import open_db
from app.storage.repositories import utc_now

PHASES = ("parsing", "routing", "indexing", "auditing", "exporting")


def management(task_id):
    with open_db() as db:
        row = db.execute(
            "SELECT payload FROM audit_management WHERE task_id=?", (task_id,)
        ).fetchone()
    return json.loads(row[0]) if row else {}


def update_management(task_id, **changes):
    with open_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT payload FROM audit_management WHERE task_id=?", (task_id,)
        ).fetchone()
        value = json.loads(row[0]) if row else {}
        value.update(changes, updated_at=utc_now())
        db.execute(
            "INSERT INTO audit_management VALUES (?,?) ON CONFLICT(task_id) DO UPDATE SET payload=excluded.payload",
            (task_id, json.dumps(value, ensure_ascii=False)),
        )
    return value


def file_rows(task_id):
    with open_db() as db:
        rows = db.execute(
            "SELECT * FROM audit_task_files WHERE task_id=? ORDER BY upload_index",
            (task_id,),
        ).fetchall()
    return [dict(row) | {"payload": json.loads(row["payload"])} for row in rows]


def file_row(task_id, file_id):
    row = next((r for r in file_rows(task_id) if r["file_id"] == file_id), None)
    if row is None:
        raise NotFoundError("audit_file_not_found", "文件不属于此审核任务")
    return row


def update_file(
    task_id, file_id, *, stage=None, stage_status=None, reason=None, **changes
):
    with open_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT payload FROM audit_task_files WHERE task_id=? AND file_id=?",
            (task_id, file_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("audit_file_not_found", "文件不属于此任务")
        value = json.loads(row[0])
        value.update(changes, updated_at=utc_now())
        if stage:
            value["phase"] = stage
            value["stages"][stage] = {"status": stage_status, "reason": reason}
        if changes.get("status") == "failed":
            for item in value["stages"].values():
                if item["status"] == "waiting":
                    item.update(status="not_run", reason="前序执行失败，未执行")
        db.execute(
            "UPDATE audit_task_files SET payload=? WHERE task_id=? AND file_id=?",
            (json.dumps(value, ensure_ascii=False), task_id, file_id),
        )
    return value


def aggregate_status(files):
    states = [f["status"] for f in files]
    if any(s in {"queued", "running"} for s in states):
        return "running"
    if not states or all(s == "failed" for s in states):
        return "failed"
    if "failed" in states:
        return "partially_failed"
    return (
        "completed_with_report_error"
        if any(f["word_status"] == "failed" or f.get("export_error") for f in files)
        else "completed"
    )


def interrupt_tasks():
    """启动时只处理未终结任务，不覆盖已发布单文件成果，不收费续跑。"""
    with open_db() as db:
        ids = [
            r[0]
            for r in db.execute(
                "SELECT task_id FROM audit_tasks WHERE status IN ('running','receiving')"
            )
        ]
        db.execute(
            "UPDATE audit_uploads SET state='failed',error_json=? WHERE state='receiving'",
            (
                json.dumps(
                    {"code": "process_interrupted", "message": "接收中断，请重新提交"}
                ),
            ),
        )
    from app.audit.local_trace import interrupt_trace

    for task_id in ids:
        for row in file_rows(task_id):
            p = row["payload"]
            if p["status"] in {"queued", "running"}:
                from app.core.errors import BusinessError
                from app.services.audit_batch_runner import fail_file

                fail_file(
                    task_id,
                    row["file_id"],
                    BusinessError("process_interrupted", "服务重启，执行中断"),
                )
        interrupt_trace(task_id)
        if management(task_id):
            update_management(task_id, completed_at=utc_now(), trace_status="partial")
        with open_db() as db:
            status = aggregate_status([r["payload"] for r in file_rows(task_id)])
            db.execute(
                "UPDATE audit_tasks SET status=?,error_code='process_interrupted',error_message='服务重启，执行中断',updated_at=? WHERE task_id=?",
                (status, utc_now(), task_id),
            )
