"""规则库管理元数据；业务规则仍以已原子保存的 RuleSet 为唯一事实源。"""

import json

from app.storage.database import open_db
from app.storage.repositories import utc_now


def management(project_id):
    with open_db() as db:
        row = db.execute(
            "SELECT payload FROM project_management WHERE project_id=?", (project_id,)
        ).fetchone()
    return json.loads(row["payload"]) if row else {}


def update_management(project_id, **changes):
    with open_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT payload FROM project_management WHERE project_id=?", (project_id,)
        ).fetchone()
        payload = json.loads(row["payload"]) if row else {}
        payload.update(changes, updated_at=utc_now())
        db.execute(
            "INSERT INTO project_management(project_id,payload) VALUES (?,?) "
            "ON CONFLICT(project_id) DO UPDATE SET payload=excluded.payload",
            (project_id, json.dumps(payload, ensure_ascii=False)),
        )
    return payload


def mark_interrupted_projects():
    """单进程启动恢复：保留已写成果，不自动重跑、重复调用模型。"""
    with open_db() as db:
        rows = db.execute(
            "SELECT project_id FROM projects WHERE status='processing_rules'"
        ).fetchall()
        db.execute(
            "UPDATE projects SET status='rule_failed',error_code='process_interrupted',"
            "error_message='服务重启，处理已中断；已保存成果可查看',updated_at=? "
            "WHERE status='processing_rules'",
            (utc_now(),),
        )
        db.execute("UPDATE project_uploads SET state='failed' WHERE state='receiving'")
    for row in rows:
        project_id = row["project_id"]
        previous = management(project_id)
        update_management(
            project_id,
            phase="failed",
            failed_phase=previous.get("phase", "received"),
            trace_status="partial",
        )
        # 开始/结束均为增量事件，中断也追加新事件，不篡改旧历史。
        from app.rules.local_trace import interrupt_trace

        interrupt_trace(project_id)
