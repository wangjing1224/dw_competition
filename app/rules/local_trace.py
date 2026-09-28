"""独立于 LangSmith 的本地展示轨迹。观察失败不影响业务；只记录白名单字段。"""

import json
import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from uuid import uuid4

from app.storage.database import open_db
from app.storage.project_management import update_management
from app.storage.repositories import utc_now

logger = logging.getLogger(__name__)
current_trace = ContextVar("rule_local_trace", default=None)


def display_value(value):
    """对展示副本截断与脱敏；不修改送模型的真实数据。"""
    truncated = False

    def visit(item):
        nonlocal truncated
        if isinstance(item, str):
            item = re.sub(r"data:image/[^\s\"']+", "[图片二进制省略]", item)
            item = re.sub(
                r"(?i)(?:sk-|lsv2_)[A-Za-z0-9_+=/-]{6,}", "[密钥已隐藏]", item
            )
            if len(item) > 20000:
                truncated = True
                item = item[:20000] + "\n[展示已截断]"
            return item
        if isinstance(item, dict):
            return {
                k: visit(v)
                for k, v in item.items()
                if k.lower()
                not in {
                    "state",
                    "messages",
                    "reasoning_content",
                    "reasoning",
                    "api_key",
                    "authorization",
                }
            }
        if isinstance(item, (list, tuple)):
            truncated |= len(item) > 100
            return [visit(v) for v in item[:100]]
        return item

    return visit(value), truncated


def append_event(project_id, node, detail):
    with open_db() as db:
        db.execute("BEGIN IMMEDIATE")
        seq = db.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM project_trace WHERE project_id=?",
            (project_id,),
        ).fetchone()[0]
        db.execute(
            "INSERT INTO project_trace VALUES (?,?,?,?,?)",
            (
                project_id,
                seq,
                node["span_id"],
                json.dumps(node, ensure_ascii=False),
                json.dumps(detail, ensure_ascii=False),
            ),
        )


class LocalTrace:
    def __init__(self, project_id):
        self.project_id = project_id
        self.nodes = {}
        self.details = {}
        self.parents = {}
        self.damaged = False

    def write(self, node, detail):
        try:
            clean, truncated = display_value(detail)
            clean["truncated"] = truncated
            append_event(self.project_id, node, clean)
        except Exception:
            self.damaged = True
            logger.warning(
                "规则库轨迹记录失败 project=%s", self.project_id, exc_info=True
            )
            try:
                update_management(self.project_id, trace_status="partial")
            except Exception:
                logger.warning(
                    "轨迹异常状态未能保存 project=%s", self.project_id, exc_info=True
                )

    def start(self, kind, title, *, parent=None, input=None, **fields):
        span_id = uuid4().hex
        node = {
            "span_id": span_id,
            "parent_span_id": parent,
            "kind": kind,
            "file_id": None,
            "batch_index": None,
            "turn_index": None,
            "name": kind,
            "title": title,
            "tool_call_id": None,
            "status": "running",
            "started_at": utc_now(),
            "finished_at": None,
            "duration_ms": None,
            "summary": "",
            "has_detail": input is not None,
        }
        node.update(fields)
        detail = {"input": input, "output": None, "error": None}
        self.nodes[span_id], self.details[span_id] = node, detail
        self.write(node, detail)
        return span_id

    def finish(self, span_id, *, output=None, error=None, status=None, summary=None):
        if not span_id or span_id not in self.nodes:
            return
        node = self.nodes[span_id]
        if node["status"] != "running":
            return
        node.update(
            status=status or ("failed" if error else "succeeded"), finished_at=utc_now()
        )
        node["duration_ms"] = round(
            (
                datetime.fromisoformat(node["finished_at"])
                - datetime.fromisoformat(node["started_at"])
            ).total_seconds()
            * 1000
        )
        node["summary"] = summary or {
            "failed": "执行失败",
            "partial": "部分查询组未完成",
        }.get(node["status"], "执行完成")
        detail = self.details[span_id]
        detail.update(
            output=output,
            error={"code": type(error).__name__, "message": str(error)}
            if error
            else None,
        )
        node["has_detail"] = any(detail.values())
        self.write(node, detail)

    def hierarchy(self, session, step=None):
        """按真实调用首次出现顺序创建父节点；压缩续跑复用原轮次。"""
        file_id = session.current.document.file_id
        keys = [(file_id, "file", session.current.document.file_name)]
        if session.batch_index:
            keys.append(
                (
                    f"{file_id}/{session.batch_index}",
                    "batch",
                    f"阅读批次 {session.batch_index}",
                )
            )
        if step:
            keys.append(
                (f"{file_id}/{session.batch_index}/{step}", "turn", f"第 {step} 轮")
            )
        parent = None
        for key, kind, title in keys:
            if key not in self.parents:
                self.parents[key] = self.start(
                    kind,
                    title,
                    parent=parent,
                    file_id=file_id,
                    batch_index=session.batch_index or None,
                    turn_index=step if kind == "turn" else None,
                )
            parent = self.parents[key]
        return parent

    def close_scope(self, session, kind, error=None):
        key = session.current.document.file_id
        if kind in {"batch", "turn"}:
            key += f"/{session.batch_index}"
        if kind == "turn":
            key += f"/{session.model_calls}"
        self.finish(self.parents.get(key), error=error)

    def close(self, error=None):
        for span in reversed(list(self.nodes)):
            self.finish(span, error=error)
        try:
            update_management(
                self.project_id, trace_status="partial" if self.damaged else "completed"
            )
        except Exception:
            logger.warning("轨迹结束状态未保存", exc_info=True)


@contextmanager
def trace_scope(session, kind, title, *, input=None, **fields):
    trace = current_trace.get()
    span = None
    result = {}
    if trace:
        step = session.model_calls + 1 if kind == "compaction" else session.model_calls
        parent = trace.hierarchy(session, step)
        span = trace.start(
            kind,
            title,
            parent=parent,
            input=input,
            file_id=session.current.document.file_id,
            batch_index=session.batch_index,
            turn_index=step or None,
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
            trace.finish(span, **result)


def observe_tool_result(
    observation, session, call, result, before_profile, commit_count
):
    """已成功调用的结果投影失败时，只损失展示，不向模型谎报工具执行失败。"""
    if current_trace.get() is None:
        return
    try:
        name = call["name"]
        output = {"content": result.content}
        if name == "view_image":
            path = call["args"]["path"]
            fid = path.split("/", 1)[0]
            parsed = {
                **session.history,
                session.current.document.file_id: session.current,
            }[fid]
            version = parsed.markdown_path.split("/")[-2]
            output = {
                "images": [
                    {
                        "file_id": fid,
                        "path": path,
                        "parse_version": version,
                        "url": f"/api/projects/{session.rule_set.project_id}/files/{fid}/preview?image_name={path.split('/')[-1]}&version={version}",
                    }
                ]
            }
        if name == "submit_rule_delta" and len(session.commits) > commit_count:
            commit = session.commits[-1]
            before = {r.rule_id: r.model_dump(mode="json") for r in commit.before}
            after = {r.rule_id: r.model_dump(mode="json") for r in commit.after}
            output.update(
                rule_changes=[
                    {
                        "rule_id": rid,
                        "before": before.get(rid),
                        "after": after.get(rid),
                        "absorbed_into": commit.absorbed_ids.get(rid),
                    }
                    for rid in dict.fromkeys([*before, *after])
                ],
                profile_change={
                    "before": before_profile,
                    "after": session.rule_set.project_profile,
                }
                if before_profile != session.rule_set.project_profile
                else None,
            )
        observation["output"] = output
        if getattr(result, "status", None) == "error":
            observation["status"] = "failed"
        elif name == "search_documents":
            content = str(result.content)
            if "状态：本组未执行或未完整完成" in content:
                observation["status"] = (
                    "partial" if "状态：检索完成" in content else "failed"
                )
            elif "状态：检索完成，本次未召回" in content:
                observation["summary"] = "本次未召回，不代表原文无要求"
    except Exception:
        current_trace.get().damaged = True
        logger.warning("工具展示投影失败，业务结果保持不变", exc_info=True)


def interrupt_trace(project_id):
    try:
        with open_db() as db:
            rows = db.execute(
                "SELECT node,detail FROM project_trace WHERE project_id=? ORDER BY seq",
                (project_id,),
            ).fetchall()
        latest = {json.loads(row["node"])["span_id"]: row for row in rows}
        for row in latest.values():
            node, detail = json.loads(row["node"]), json.loads(row["detail"])
            if node["status"] == "running":
                node.update(
                    status="interrupted", finished_at=utc_now(), summary="服务中断"
                )
                detail["error"] = {
                    "code": "process_interrupted",
                    "message": "服务重启，执行中断",
                }
                append_event(project_id, node, detail)
    except Exception:
        logger.warning("中断轨迹未能闭合 project=%s", project_id, exc_info=True)
