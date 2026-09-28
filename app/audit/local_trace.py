"""审核只读观测：ContextVar 隔离并发文件/子会话，不写提示词及隐藏推理。"""

import json
import logging
import re
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from functools import wraps
from uuid import uuid4

from app.rules.local_trace import LocalTrace, display_value
from app.storage.audit_management import update_management
from app.storage.database import open_db
from app.storage.repositories import utc_now

logger = logging.getLogger(__name__)
current = ContextVar("audit_local_trace", default=None)


def safe_value(value):
    clean, truncated = display_value(value)

    def redact(item):
        if isinstance(item, str):
            return re.sub(
                r"(?:/Users/|/home/|/private/|/tmp/)[^\s\"']+",
                "[服务器路径已隐藏]",
                item,
            )
        if isinstance(item, dict):
            return {k: redact(v) for k, v in item.items()}
        if isinstance(item, list):
            # 部分模型把隐藏推理放在 content blocks，而不是 reasoning_content。
            return [
                redact(v)
                for v in item
                if not (
                    isinstance(v, dict)
                    and v.get("type") in {"reasoning", "thinking", "redacted_thinking"}
                )
            ]
        return item

    return redact(clean), truncated


def append_event(task_id, node, detail):
    with open_db() as db:
        db.execute("BEGIN IMMEDIATE")
        seq = db.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM audit_trace WHERE task_id=?", (task_id,)
        ).fetchone()[0]
        db.execute(
            "INSERT INTO audit_trace VALUES (?,?,?,?,?)",
            (
                task_id,
                seq,
                node["span_id"],
                json.dumps(node, ensure_ascii=False),
                json.dumps(detail, ensure_ascii=False),
            ),
        )


class AuditTrace(LocalTrace):
    def __init__(self, task_id, file_id):
        self.task_id, self.file_id = task_id, file_id
        self.nodes, self.details, self.parents = {}, {}, {}
        self.damaged = False

    def write(self, node, detail):
        try:
            clean, truncated = safe_value(detail)
            clean["truncated"] = truncated
            node = safe_value(node)[0] | {
                "task_id": self.task_id,
                "file_id": self.file_id,
            }
            node["status"] = (
                "completed" if node["status"] == "succeeded" else node["status"]
            )
            node["ended_at"] = node.get("finished_at")
            append_event(self.task_id, node, clean)
        except Exception:
            self.damaged = True
            logger.warning(
                "审核轨迹记录失败 task=%s file=%s",
                self.task_id,
                self.file_id,
                exc_info=True,
            )
            try:
                update_management(self.task_id, trace_status="partial")
            except Exception:
                logger.warning("轨迹异常状态未保存", exc_info=True)


@contextmanager
def bind(trace):
    token = current.set((trace, None, {"actor": "system"}))
    try:
        yield
    finally:
        current.reset(token)


@contextmanager
def scope(kind, title, *, input=None, **fields):
    value, output = current.get(), {}
    if value is None:
        yield output
        return
    trace, parent, inherited = value
    fields = inherited | fields
    span = trace.start(kind, title, parent=parent, input=input, **fields)
    token = current.set((trace, span, fields))
    try:
        yield output
    except BaseException as exc:
        trace.finish(span, error=exc)
        raise
    else:
        trace.finish(span, **output)
    finally:
        current.reset(token)


@contextmanager
def turn(step):
    value = current.get()
    if value is None:
        yield
        return
    trace, parent, fields = value
    key = (fields.get("session_id"), step)
    if key not in trace.parents:
        trace.parents[key] = trace.start(
            "turn", f"第 {step} 轮", parent=parent, **(fields | {"step": step})
        )
    token = current.set((trace, trace.parents[key], fields | {"step": step}))
    try:
        yield
    except BaseException as exc:
        # 预算控制抛出的是续跑信号；模型尚未调用，不算失败轮次。
        from app.llm.context_compaction import NeedsCompaction

        if not isinstance(exc, NeedsCompaction):
            trace.finish(trace.parents[key], error=exc)
        raise
    finally:
        current.reset(token)


def finish_turn():
    value = current.get()
    if value:
        value[0].finish(value[1])


def observed_session(function):
    @wraps(function)
    async def wrapped(**kwargs):
        role = kwargs["role"]
        meta = (kwargs.get("trace_context") or {}).get("metadata") or {}
        with ExitStack() as stack:
            if role == "evidence_subagent":
                stack.enter_context(
                    scope(
                        "evidence_check",
                        f"资料核验 · {meta.get('rule_id', '')}",
                        input={
                            "verification_request": meta.get("verification_request"),
                            "file_ids": meta.get("target_file_ids", []),
                        },
                        actor=role,
                        rule_id=meta.get("rule_id"),
                        check_index=meta.get("check_index"),
                        target_file_ids=meta.get("target_file_ids", []),
                    )
                )
            stack.enter_context(
                scope(
                    "batch",
                    "主审核会话" if role == "bid_agent" else "资料核验会话",
                    actor=role,
                    session_id=uuid4().hex,
                    batch_index=meta.get("batch_index"),
                )
            )
            return await function(**kwargs)

    return wrapped


def observed_turn(function):
    @wraps(function)
    async def wrapped(state, **kwargs):
        with turn(state["model_calls"] + 1):
            result = await function(state, **kwargs)
            if not getattr(result["messages"][-1], "tool_calls", None):
                finish_turn()
            return result

    return wrapped


def interrupt_trace(task_id):
    with open_db() as db:
        rows = db.execute(
            "SELECT node,detail FROM audit_trace WHERE task_id=? ORDER BY seq",
            (task_id,),
        ).fetchall()
    latest = {json.loads(r["node"])["span_id"]: r for r in rows}
    for row in latest.values():
        node = json.loads(row["node"])
        if node["status"] == "running":
            node.update(
                status="interrupted", ended_at=utc_now(), summary="服务重启，执行中断"
            )
            append_event(task_id, node, json.loads(row["detail"]))
