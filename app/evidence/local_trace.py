"""资料入库旁路轨迹：复用节点计时/层级算法，存储和业务投影按 bidder 隔离。"""

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar

from app.rules.local_trace import LocalTrace, display_value
from app.storage.bidder_management import update_management
from app.storage.database import open_db
from app.storage.repositories import utc_now

logger = logging.getLogger(__name__)
current_trace = ContextVar("evidence_local_trace", default=None)


def append_event(bidder_id, node, detail):
    with open_db() as db:
        db.execute("BEGIN IMMEDIATE")
        seq = db.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM bidder_trace WHERE bidder_id=?",
            (bidder_id,),
        ).fetchone()[0]
        db.execute(
            "INSERT INTO bidder_trace VALUES (?,?,?,?,?)",
            (
                bidder_id,
                seq,
                node["span_id"],
                json.dumps(node, ensure_ascii=False),
                json.dumps(detail, ensure_ascii=False),
            ),
        )


class EvidenceTrace(LocalTrace):
    def __init__(self, bidder_id):
        self.bidder_id = bidder_id
        self.nodes, self.details, self.parents = {}, {}, {}
        self.damaged = False

    def write(self, node, detail):
        try:
            clean, truncated = display_value(detail)
            clean["truncated"] = truncated
            append_event(self.bidder_id, display_value(node)[0], clean)
        except Exception:
            self.damaged = True
            logger.warning("资料轨迹记录失败 bidder=%s", self.bidder_id, exc_info=True)
            try:
                update_management(self.bidder_id, trace_status="partial")
            except Exception:
                logger.warning("资料轨迹状态未保存", exc_info=True)

    def close(self, error=None):
        for span in reversed(list(self.nodes)):
            self.finish(span, error=error)
        try:
            update_management(
                self.bidder_id,
                trace_status="partial"
                if self.damaged
                else "failed"
                if error
                else "completed",
            )
        except Exception:
            logger.warning("资料轨迹结束状态未保存", exc_info=True)


@contextmanager
def trace_scope(session, kind, title, *, input=None, **fields):
    trace = current_trace.get()
    result, span = {}, None
    if trace:
        step = session.model_calls + 1 if kind == "compaction" else session.model_calls
        span = trace.start(
            kind,
            title,
            parent=trace.hierarchy(session, step),
            file_id=session.file_id,
            batch_index=session.batch_index,
            turn_index=step or None,
            input=input,
            **fields,
        )
    try:
        yield result
    except BaseException as exc:
        if trace:
            trace.finish(span, error=exc)
        raise
    else:
        if trace:
            # 保存变更与工具正文分开供界面展示，不修改给模型的结果。
            change = result.pop("material_change", None)
            if change:
                trace.details[span]["material_change"] = change
            trace.finish(span, **result)


def close_scope(session, kind, error=None):
    trace = current_trace.get()
    if trace:
        trace.close_scope(session, kind, error)


def observe_tool_result(observation, session, call, result, before):
    """工具已经返回，展示投影即使失败也不能改变给模型的 ToolMessage。"""
    if current_trace.get() is None:
        return
    try:
        name = call["name"]
        observation["output"] = result.content
        if getattr(result, "status", None) == "error":
            observation["status"] = "failed"
            return
        if name == "view_image":
            path = call["args"]["path"]
            file_id = path.split("/")[0]
            parsed = {**session.history, session.file_id: session.current}[file_id]
            if not any(
                image.path == path
                for section in parsed.sections
                for block in section.blocks
                for image in block.images
            ):
                raise ValueError("图片未在会话解析快照中登记")
            version = parsed.markdown_path.split("/")[-2]
            observation["output"] = {
                "images": [
                    {
                        "path": path,
                        "url": f"/api/bidders/{session.library.bidder_id}/files/{file_id}/preview?image_name={path.split('/')[-1]}&version={version}",
                    }
                ]
            }
        elif name == "search_documents" and "状态：本组未执行或未完整完成" in str(
            result.content
        ):
            observation["status"] = "partial"
        elif (
            name == "save_evidence_material"
            and call["args"].get("material") is not None
        ):
            after = next(
                (
                    m.model_dump(mode="json")
                    for m in session.library.materials
                    if m.file_id == session.file_id
                ),
                None,
            )
            if before != after:
                observation["material_change"] = {
                    "file_id": session.file_id,
                    "before": before,
                    "after": after,
                }
                observation["summary"] = "已保存当前文件摘要及关系"
    except Exception:
        # 禁止意外把看图的 Base64 原始结果落到展示轨迹。
        observation["output"] = None
        observation["summary"] = "工具已返回，展示详情暂不可用"
        current_trace.get().damaged = True
        logger.warning("资料工具展示投影失败", exc_info=True)


def interrupt_trace(bidder_id):
    with open_db() as db:
        rows = db.execute(
            "SELECT node,detail FROM bidder_trace WHERE bidder_id=? ORDER BY seq",
            (bidder_id,),
        ).fetchall()
    latest = {json.loads(r["node"])["span_id"]: r for r in rows}
    for row in latest.values():
        node = json.loads(row["node"])
        if node["status"] == "running":
            node.update(
                status="interrupted",
                finished_at=utc_now(),
                summary="服务重启，执行中断",
            )
            append_event(bidder_id, node, json.loads(row["detail"]))
