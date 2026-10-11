"""审核管理 API 的只读投影与上传受理；构造此服务不会初始化模型客户端。"""

import hashlib
import json
from contextlib import ExitStack
from pathlib import PurePosixPath
from urllib.parse import urlencode

from pydantic import Field

from app.audit.context import fixed_parsed, verify_indexes
from app.audit.local_trace import safe_value
from app.core.config import get_settings
from app.core.errors import BusinessError, NotFoundError
from app.models.common import StrictModel
from app.models.documents import ParsedDocument
from app.models.evidence import EvidenceLibrary
from app.models.results import AuditResult
from app.models.rules import RuleReportItem, RuleSet, present_rule_for_model
from app.parsers.assets import atomic_write
from app.prompts.loader import load_prompt
from app.services.evidence_library_service import EvidenceLibraryService
from app.services.rule_library_service import (
    UPLOAD_EXTENSIONS,
    RuleLibraryService,
    _locks,
)
from app.services.source_preview import SourcePreviewService
from app.storage.audit_management import (
    PHASES,
    file_row,
    file_rows,
    management,
    update_management,
)
from app.storage.database import open_db
from app.storage.file_store import FileOwner, FileStore, normalize_relative_path
from app.storage.ids import next_id
from app.storage.library_names import lock_library_names
from app.storage.repositories import AuditTaskRepository, FileRepository, utc_now


class Assignment(StrictModel):
    upload_index: int = Field(ge=0, strict=True)
    project_id: str = Field(pattern=r"^project-[0-9]+$")
    bidder_id: str = Field(pattern=r"^bidder-[0-9]+$")
    project_revision: str = Field(min_length=1)
    bidder_revision: str = Field(min_length=1)


class AuditManagementService:
    def __init__(self):
        self.settings, self.store = get_settings(), FileStore()
        self.files, self.tasks = FileRepository(), AuditTaskRepository()
        self.sources = SourcePreviewService(self.settings, self.store)

    def snapshot_path(self, task_id):
        return self.store.task_output_dir(task_id) / "batch_context.json"

    def snapshot(self, task_id):
        self.tasks.require(task_id)
        try:
            return json.loads(self.snapshot_path(task_id).read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise BusinessError(
                "snapshot_unavailable", "审核依据快照不可读取", http_status=409
            ) from exc

    def freeze(self, assignments):
        """与人工维护使用相同锁；冻结的是当前审核实际使用的库与解析版本。"""
        rule_service, evidence_service = RuleLibraryService(), EvidenceLibraryService()
        projects, bidders, parsed = {}, {}, {}
        with ExitStack() as stack:
            for pid in sorted({a.project_id for a in assignments}):
                stack.enter_context(_locks[pid])
            for bid in sorted({a.bidder_id for a in assignments}):
                stack.enter_context(evidence_service.lock(bid))
            for a in assignments:
                for kind, key, expected, service, cache in (
                    (
                        "project",
                        a.project_id,
                        a.project_revision,
                        rule_service,
                        projects,
                    ),
                    (
                        "bidder",
                        a.bidder_id,
                        a.bidder_revision,
                        evidence_service,
                        bidders,
                    ),
                ):
                    detail = cache.get(key)
                    if detail is None:
                        detail = service.detail(key)
                        cache[key] = detail
                    if detail["revision"] != expected:
                        raise BusinessError(
                            "library_revision_conflict",
                            "所选库已更新，请刷新并确认",
                            http_status=409,
                            details={
                                "upload_indices": [
                                    x.upload_index
                                    for x in assignments
                                    if getattr(x, kind + "_id") == key
                                ]
                            },
                        )
                    if not detail["available_for_audit"]:
                        raise BusinessError(
                            "library_not_ready",
                            "所选规则库或资料库尚不可用于审核",
                            http_status=409,
                        )
            for pid, detail in projects.items():
                rules = RuleSet.model_validate(detail["rule_set"])
                parsed.update(
                    fixed_parsed(
                        rules.source_files,
                        self.files,
                        role="rule_source",
                        project_id=pid,
                    )
                )
            for bid, detail in bidders.items():
                lib = EvidenceLibrary.model_validate(detail["library"])
                parsed.update(
                    fixed_parsed(
                        lib.source_files,
                        self.files,
                        role="evidence_material",
                        bidder_id=bid,
                    )
                )
            verify_indexes(parsed, self.settings)
        return {
            "projects": {
                key: {
                    "name": d["project"]["display_name"],
                    "revision": d["revision"],
                    "rule_set": d["rule_set"],
                }
                for key, d in projects.items()
            },
            "bidders": {
                key: {
                    "name": d["bidder"]["display_name"],
                    "revision": d["revision"],
                    "library": d["library"],
                }
                for key, d in bidders.items()
            },
            "parsed": {
                key: value.model_dump(mode="json") for key, value in parsed.items()
            },
            "report_template": load_prompt("audit_report_template"),
            "report_format_version": 3,
        }

    def receive(self, uploads, name, request_id, assignments):
        name = name.strip()
        if not 1 <= len(name) <= 100 or not uploads:
            raise BusinessError(
                "invalid_submission",
                "名称须为1～100字符且至少上传一份文件",
                http_status=422,
            )
        if sorted(a.upload_index for a in assignments) != list(range(len(uploads))):
            raise BusinessError(
                "invalid_assignments", "每份文件必须恰好指定一组审核库", http_status=422
            )
        assignments = sorted(assignments, key=lambda a: a.upload_index)
        digest, seen = hashlib.sha256(name.encode()), set()
        for (filename, path, stream), assignment in zip(
            uploads, assignments, strict=True
        ):
            normalized = normalize_relative_path(path)
            if normalized in seen:
                raise BusinessError(
                    "duplicate_relative_path", "同批文件路径不能重复", http_status=422
                )
            seen.add(normalized)
            if PurePosixPath(filename).suffix.lower() not in UPLOAD_EXTENSIONS:
                raise BusinessError(
                    "unsupported_file_type", "不支持的文件格式", http_status=422
                )
            size, content_hash = 0, hashlib.sha256()
            stream.seek(0)
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > self.settings.storage.max_upload_bytes:
                    raise BusinessError(
                        "file_too_large", "文件超过上传大小限制", http_status=413
                    )
                content_hash.update(chunk)
            stream.seek(0)
            if not size:
                raise BusinessError("empty_file", "不能上传空文件", http_status=422)
            digest.update(
                json.dumps(
                    [
                        filename,
                        normalized,
                        content_hash.hexdigest(),
                        assignment.model_dump(),
                    ],
                    sort_keys=True,
                ).encode()
            )
        with lock_library_names("audit"):
            with open_db() as db:
                prior = db.execute(
                    "SELECT * FROM audit_uploads WHERE request_id=?", (request_id,)
                ).fetchone()
                if prior:
                    if prior["fingerprint"] != digest.hexdigest():
                        raise BusinessError(
                            "request_conflict",
                            "提交标识已用于不同内容",
                            http_status=409,
                        )
                    if prior["state"] == "accepted":
                        return self.creation(prior["task_id"], request_id, True), False
                    raise BusinessError(
                        "receiving"
                        if prior["state"] == "receiving"
                        else "upload_failed",
                        "此提交正在接收或已失败，请查询状态后明确重新提交",
                        http_status=409,
                    )
                for row in db.execute("SELECT payload FROM audit_management"):
                    if (
                        json.loads(row[0]).get("display_name", "").strip().casefold()
                        == name.casefold()
                    ):
                        raise BusinessError(
                            "duplicate_name", "审核任务名称已存在", http_status=409
                        )
            frozen = self.freeze(assignments)
            frozen["task_name"] = name
            task_id, now = next_id("audit"), utc_now()
            with open_db() as db:
                db.execute(
                    "INSERT INTO audit_tasks(task_id,status,created_at,updated_at) VALUES (?,'receiving',?,?)",
                    (task_id, now, now),
                )
                db.execute(
                    "INSERT INTO audit_uploads VALUES (?,?,?,'receiving',NULL)",
                    (request_id, digest.hexdigest(), task_id),
                )
            update_management(
                task_id,
                display_name=name,
                client_request_id=request_id,
                trace_status="complete",
                created_at=now,
            )
            try:
                rows = []
                for (filename, path, stream), a in zip(
                    uploads, assignments, strict=True
                ):
                    record = self.store.save(
                        owner=FileOwner(a.project_id, a.bidder_id, task_id),
                        data_role="bid_document",
                        stream=stream,
                        file_name=filename,
                        relative_path=path,
                    )
                    payload = {
                        "status": "queued",
                        "phase": "queued",
                        "failed_phase": None,
                        "project_name": frozen["projects"][a.project_id]["name"],
                        "project_revision": a.project_revision,
                        "bidder_name": frozen["bidders"][a.bidder_id]["name"],
                        "bidder_revision": a.bidder_revision,
                        "stages": {
                            phase: {"status": "waiting", "reason": None}
                            for phase in PHASES
                        },
                        "route": None,
                        "word_status": "unavailable",
                        "error": None,
                        "export_error": None,
                        "report_kind": "none",
                        "completed_at": None,
                        "updated_at": now,
                    }
                    rows.append(
                        (
                            task_id,
                            record.file_id,
                            a.upload_index,
                            a.project_id,
                            a.bidder_id,
                            json.dumps(payload, ensure_ascii=False),
                        )
                    )
                atomic_write(
                    self.snapshot_path(task_id),
                    json.dumps(frozen, ensure_ascii=False).encode(),
                )
                # 只有全部原件及快照就绪后才发布明细并允许后台启动。
                with open_db() as db:
                    db.executemany(
                        "INSERT INTO audit_task_files VALUES (?,?,?,?,?,?)", rows
                    )
                    db.execute(
                        "UPDATE audit_tasks SET status='running' WHERE task_id=?",
                        (task_id,),
                    )
                    db.execute(
                        "UPDATE audit_uploads SET state='accepted' WHERE request_id=?",
                        (request_id,),
                    )
            except BaseException:
                with open_db() as db:
                    db.execute(
                        "UPDATE audit_tasks SET status='failed',error_code='upload_failed',error_message='接收失败，未启动审核' WHERE task_id=?",
                        (task_id,),
                    )
                    db.execute(
                        "UPDATE audit_uploads SET state='failed',error_json=? WHERE request_id=?",
                        (
                            json.dumps(
                                {
                                    "code": "upload_failed",
                                    "message": "接收失败，未启动审核",
                                }
                            ),
                            request_id,
                        ),
                    )
                raise
        return self.creation(task_id, request_id, False), True

    def creation(self, task_id, request_id, reused):
        return {
            "task_id": task_id,
            "status": self.tasks.require(task_id).status,
            "client_request_id": request_id,
            "reused": reused,
            "files": [
                {"upload_index": r["upload_index"], "file_id": r["file_id"]}
                for r in file_rows(task_id)
            ],
        }

    def file_view(self, row):
        p = dict(row["payload"])
        record = self.files.require(row["file_id"])
        if (record.task_id, record.project_id, record.bidder_id) != (
            row["task_id"],
            row["project_id"],
            row["bidder_id"],
        ):
            raise BusinessError(
                "binding_corrupt", "文件归属与审核明细不一致", http_status=500
            )
        base = f"/api/audit-tasks/{row['task_id']}/outputs?file_id={row['file_id']}&format="
        draft = (
            self.store.task_output_dir(row["task_id"])
            / "drafts"
            / f"{row['file_id']}.md"
        )
        revision = (
            hashlib.sha256(draft.read_bytes()).hexdigest() if draft.exists() else None
        )
        return {
            **{k: v for k, v in p.items() if not k.endswith("_path")},
            "file_id": record.file_id,
            "file_name": record.file_name,
            "source_path": record.source_path,
            "size_bytes": record.size_bytes,
            "extension": record.extension,
            **{k: row[k] for k in ("upload_index", "project_id", "bidder_id")},
            "stages": [{"phase": phase, **p["stages"][phase]} for phase in PHASES],
            "report_kind": "final"
            if p.get("json_path")
            else "draft"
            if revision
            else "none",
            "report_revision": revision,
            "overall_conclusion": p.get("overall_conclusion"),
            "outputs": {
                kind + "_url": base + kind if p.get(kind + "_path") else None
                for kind in ("json", "md", "word")
            },
        }

    def batch_view(self, task_id):
        task, meta = self.tasks.require(task_id), management(task_id)
        rows = file_rows(task_id)
        states = [r["payload"]["status"] for r in rows]
        with open_db() as db:
            times = dict(
                db.execute(
                    "SELECT created_at,updated_at FROM audit_tasks WHERE task_id=?",
                    (task_id,),
                ).fetchone()
            )
        return {
            "task_id": task_id,
            "display_name": meta.get("display_name", task_id),
            "status": task.status,
            "total_files": len(rows),
            "finished_files": sum(s in {"completed", "failed"} for s in states),
            **{
                key + "_files": states.count(status)
                for key, status in (
                    ("completed", "completed"),
                    ("failed", "failed"),
                    ("running", "running"),
                    ("queued", "queued"),
                )
            },
            "project_count": len({r["project_id"] for r in rows}),
            "bidder_count": len({r["bidder_id"] for r in rows}),
            "created_at": times["created_at"],
            "updated_at": max(
                [
                    times["updated_at"],
                    meta.get("updated_at", ""),
                    *[r["payload"]["updated_at"] for r in rows],
                ]
            ),
            "completed_at": meta.get("completed_at"),
            "legacy": not bool(meta),
            "error": {
                "code": task.error_code,
                "message": safe_value(task.error_message)[0],
                "file_id": None,
            }
            if task.error_code
            else None,
            "outputs": {
                "batch_json_url": f"/api/audit-tasks/{task_id}/outputs?format=json"
                if task.json_path
                else None
            },
        }

    def list(self, page=1, page_size=12, q=None, group="all", client_request_id=None):
        if client_request_id and (q or group != "all"):
            raise BusinessError(
                "invalid_query", "提交恢复不能同时使用名称或状态筛选", http_status=422
            )
        with open_db() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT task_id FROM audit_tasks WHERE status!='receiving' ORDER BY created_at DESC,task_id DESC"
                )
            ]
            request = (
                db.execute(
                    "SELECT * FROM audit_uploads WHERE request_id=?",
                    (client_request_id,),
                ).fetchone()
                if client_request_id
                else None
            )
        items = [self.batch_view(key) for key in ids]
        overview = {
            "total": len(items),
            "running": sum(i["status"] == "running" for i in items),
            "completed": sum(i["status"] == "completed" for i in items),
        }
        overview["attention"] = (
            overview["total"] - overview["running"] - overview["completed"]
        )
        if client_request_id:
            items = [
                i
                for i in items
                if request
                and request["state"] == "accepted"
                and i["task_id"] == request["task_id"]
            ]
        else:
            items = [
                i
                for i in items
                if (
                    not q
                    or q.casefold()
                    in (i["display_name"] + " " + i["task_id"]).casefold()
                )
                and (
                    group == "all"
                    or i["status"] == group
                    or group == "attention"
                    and i["status"] not in {"running", "completed"}
                )
            ]
        return {
            "items": items[(page - 1) * page_size : page * page_size],
            "page": page,
            "page_size": page_size,
            "total": len(items),
            "overview": overview,
            "request_state": request["state"]
            if request
            else "not_found"
            if client_request_id
            else None,
            "request_error": json.loads(request["error_json"])
            if request and request["error_json"]
            else None,
        }

    def detail(self, task_id):
        return {
            "task": self.batch_view(task_id),
            "files": [self.file_view(r) for r in file_rows(task_id)],
        }

    def report(self, task_id, file_id):
        row = file_row(task_id, file_id)
        view = self.file_view(row)
        frozen = self.snapshot(task_id)
        rules = frozen["projects"][row["project_id"]]["rule_set"]["rules"]
        result, counts, links = None, None, []
        if row["payload"].get("json_path"):
            try:
                envelope = AuditResult.model_validate_json(
                    self.store.resolve_relative(
                        row["payload"]["json_path"]
                    ).read_bytes()
                )
                value = envelope.file_results[0]
                if (
                    len(envelope.file_results) != 1
                    or value.file.file_id != file_id
                    or envelope.task_id != task_id
                    or (envelope.project_id, envelope.bidder_id)
                    != (row["project_id"], row["bidder_id"])
                ):
                    raise ValueError("报告身份错误")
                expected_rules = RuleSet.model_validate(
                    frozen["projects"][row["project_id"]]["rule_set"]
                )
                if envelope.rules != [
                    RuleReportItem.model_validate(
                        present_rule_for_model(r).model_dump()
                    )
                    for r in expected_rules.rules
                ] or envelope.evidence_library != EvidenceLibrary.model_validate(
                    frozen["bidders"][row["bidder_id"]]["library"]
                ):
                    raise ValueError("报告依据与本次冻结版本不一致")
                if value.report_format_version != frozen.get(
                    "report_format_version", 1
                ):
                    raise ValueError("报告格式与受理快照不一致")
                if value.report_format_version in (2, 3):
                    context = value.report_context
                    if (
                        not context
                        or not context.finalized_at
                        or (
                            context.task_name,
                            context.rule_library_name,
                            context.evidence_library_name,
                        )
                        != (
                            frozen.get("task_name"),
                            frozen["projects"][row["project_id"]]["name"],
                            frozen["bidders"][row["bidder_id"]]["name"],
                        )
                    ):
                        raise ValueError("报告上下文与受理快照不一致")
                result = value.model_dump(mode="json")
                if value.is_applicable:
                    counts = {
                        k: sum(str(f.result) == k for f in value.findings)
                        for k in ("compliant", "non_compliant", "manual_review")
                    }
                    counts["total"] = len(value.findings)
                for key, src in value.sources.items():
                    base = f"/api/audit-tasks/{task_id}/files/{file_id}/sources/{src.file_id}"
                    links.append(
                        {
                            "source_key": key,
                            "file_id": src.file_id,
                            "preview_url": base
                            + "/preview?"
                            + urlencode({"source_key": key}),
                            "download_url": base + "/download",
                        }
                    )
            except (OSError, ValueError, IndexError) as exc:
                raise BusinessError(
                    "report_unavailable", "正式报告不可读取", http_status=500
                ) from exc
        path = self.store.task_output_dir(task_id) / "drafts" / f"{file_id}.md"
        display_markdown = None
        try:
            markdown = (
                path.read_text("utf-8") if view["report_kind"] != "none" else None
            )
            if (
                result
                and result.get("report_format_version") in (2, 3)
                and row["payload"].get("md_path")
            ):
                display_markdown = self.store.resolve_relative(
                    row["payload"]["md_path"]
                ).read_text("utf-8")
        except OSError as exc:
            raise BusinessError(
                "report_unavailable", "已登记报告正文不可读取", http_status=500
            ) from exc
        return {
            "file": view,
            "document": {
                "kind": view["report_kind"],
                "markdown": markdown,
                "display_markdown": display_markdown,
                "revision": view["report_revision"],
                "updated_at": view["updated_at"] if markdown else None,
            },
            "result": result,
            "rules": rules,
            "counts": counts,
            "source_links": links,
        }

    def source_record(self, task_id, file_id, source_file_id):
        row = file_row(task_id, file_id)
        frozen = self.snapshot(task_id)
        allowed = (
            {file_id}
            | {
                f["file_id"]
                for f in frozen["projects"][row["project_id"]]["rule_set"][
                    "source_files"
                ]
            }
            | {
                f["file_id"]
                for f in frozen["bidders"][row["bidder_id"]]["library"]["source_files"]
            }
        )
        if source_file_id not in allowed:
            raise NotFoundError("source_not_found", "来源不属于此文件审核范围")
        record = self.files.require(source_file_id)
        if source_file_id == file_id:
            valid = (
                record.task_id,
                record.project_id,
                record.bidder_id,
                record.data_role,
            ) == (task_id, row["project_id"], row["bidder_id"], "bid_document")
        else:
            valid = record.task_id is None and (
                record.data_role == "rule_source"
                and record.project_id == row["project_id"]
                or record.data_role == "evidence_material"
                and record.bidder_id == row["bidder_id"]
            )
        if not valid:
            raise NotFoundError("source_not_found", "来源不属于此文件审核范围")
        return record

    def source(self, task_id, file_id, source_file_id):
        record = self.source_record(task_id, file_id, source_file_id)
        row = file_row(task_id, file_id)
        frozen = self.snapshot(task_id)
        data = frozen["parsed"].get(source_file_id)
        if source_file_id == file_id:
            parse_path = row["payload"].get("parsed_path")
            data = (
                json.loads(self.store.resolve_relative(parse_path).read_bytes())
                if parse_path
                else None
            )
        if data is None:
            raise BusinessError(
                "preview_not_ready", "本次任务尚无该文件的解析快照", http_status=409
            )
        parsed = ParsedDocument.model_validate(data)
        if not parsed.markdown_path:
            raise BusinessError(
                "preview_unavailable", "解析版本未登记", http_status=409
            )
        return record, parsed.markdown_path.split("/")[-2]

    def preview(
        self,
        task_id,
        file_id,
        source_file_id,
        image_name=None,
        version=None,
        source_key=None,
    ):
        record, frozen_version = self.source(task_id, file_id, source_file_id)
        if version is not None and version != frozen_version:
            raise NotFoundError("parse_version_not_found", "版本不属于本次审核")
        if image_name is not None:
            if not version:
                raise BusinessError(
                    "invalid_image_parameters", "读取图片须指定版本", http_status=422
                )
            return self.sources.image(record, image_name, version)
        base = f"/api/audit-tasks/{task_id}/files/{file_id}/sources/{source_file_id}/preview"
        result = self.sources.preview(record, base, frozen_version)
        result["reference"] = None
        if source_key:
            source = (
                (self.report(task_id, file_id)["result"] or {})
                .get("sources", {})
                .get(source_key)
            )
            if (
                source is None
                or source["file_id"] != source_file_id
                or source["parse_version"].split("/")[-2] != frozen_version
            ):
                raise NotFoundError("source_not_found", "引用未记录在此文件正式结果中")
            result["reference"] = {"source_key": source_key, **source}
        return result

    def output(self, task_id, format, file_id=None):
        task = self.tasks.require(task_id)
        if file_id:
            row = file_row(task_id, file_id)
            path = row["payload"].get(format + "_path")
        else:
            if format != "json":
                raise BusinessError(
                    "file_id_required", "下载单文件报告须指定file_id", http_status=422
                )
            path = task.json_path
        if not path:
            if file_id:
                error = row["payload"].get("export_error") or {}
                expected = {
                    "md": "markdown_export_failed",
                    "word": "word_export_failed",
                }.get(format)
                if expected and error.get("code") == expected:
                    raise BusinessError(expected, error["message"], http_status=409)
            raise BusinessError("output_not_ready", "该报告尚未生成", http_status=409)
        value = self.store.resolve_relative(path)
        if not value.is_file():
            raise BusinessError(
                "output_unavailable", "已登记报告文件不可读取", http_status=500
            )
        return value

    def progress(
        self, task_id, include_trace=False, file_id=None, after_seq=0, limit=100
    ):
        result = self.detail(task_id)
        if file_id:
            file_row(task_id, file_id)
        with open_db() as db:
            rows = db.execute(
                "SELECT seq,node FROM audit_trace WHERE task_id=? ORDER BY seq",
                (task_id,),
            ).fetchall()
        updates = [{"seq": r["seq"], "span": json.loads(r["node"])} for r in rows]
        selected = [
            r for r in updates if not file_id or r["span"]["file_id"] == file_id
        ]
        latest = {r["span"]["span_id"]: r["span"] for r in selected}
        observation = management(task_id).get("trace_status", "unavailable")
        pending = [r for r in selected if r["seq"] > after_seq]
        page = pending[:limit] if include_trace else []
        has_more = include_trace and len(pending) > limit
        next_seq = (
            (
                page[-1]["seq"]
                if has_more
                else max([after_seq, *[r["seq"] for r in updates]])
            )
            if include_trace
            else after_seq
        )
        activity = {
            name: sum(n["kind"] == kind for n in latest.values())
            if observation != "unavailable"
            else None
            for name, kind in (
                ("model_calls", "model"),
                ("tool_calls", "tool"),
                ("evidence_checks", "evidence_check"),
                ("report_saves", "report"),
            )
        }
        return result | {
            "activity": activity
            | {
                "scope": "file" if file_id else "batch",
                "file_id": file_id,
                "observation": observation,
            },
            "trace_updates": page,
            "next_seq": next_seq,
            "has_more": has_more,
            "trace_warning": None
            if observation == "complete"
            else {
                "code": "trace_partial",
                "message": "轨迹记录不完整；统计仅代表已观测调用",
            },
        }

    def trace_detail(self, task_id, span_id):
        self.tasks.require(task_id)
        with open_db() as db:
            row = db.execute(
                "SELECT node,detail FROM audit_trace WHERE task_id=? AND span_id=? ORDER BY seq DESC LIMIT 1",
                (task_id, span_id),
            ).fetchone()
        if not row:
            raise NotFoundError("trace_not_found", "轨迹节点不存在")
        return {"span": json.loads(row["node"]), **json.loads(row["detail"])}
