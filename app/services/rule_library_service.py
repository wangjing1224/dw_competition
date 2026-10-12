"""规则库管理读写，不构造模型、不发起解析或重新建立索引。"""

import hashlib
import json
import logging
import threading
from collections import defaultdict
from pathlib import PurePosixPath

from app.core.config import get_settings
from app.core.errors import BusinessError, NotFoundError
from app.models.rules import RuleExtractionProgress, RuleItem, RuleSet, RuleSourceFile
from app.parsers.assets import atomic_write
from app.retrieval.index import embedding_config_fingerprint
from app.retrieval.store import load_index
from app.rules.context import CATEGORY_LABELS, build_rule_view
from app.rules.local_trace import display_value
from app.rules.policies import normalize_priority_verification
from app.services.source_preview import SourcePreviewService
from app.storage.database import open_db
from app.storage.file_store import FileOwner, FileStore, normalize_relative_path
from app.storage.ids import next_id
from app.storage.library_names import (
    default_library_name,
    ensure_library_name_available,
    lock_library_names,
)
from app.storage.project_management import management, update_management
from app.storage.repositories import FileRepository, ProjectRepository, utc_now

_locks = defaultdict(threading.Lock)
_index_checks = {}
_index_check_lock = threading.Lock()
logger = logging.getLogger(__name__)
UPLOAD_EXTENSIONS = {
    ".doc",
    ".docx",
    ".pdf",
    ".xlsx",
    ".txt",
    ".md",
    ".png",
    ".jpg",
    ".jpeg",
}


def revision_of(payload):
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class RuleLibraryService:
    def __init__(self):
        self.settings = get_settings()
        self.store = FileStore()
        self.projects = ProjectRepository()
        self.files = FileRepository()

    def receive(self, uploads, display_name, request_id):
        with lock_library_names("project"):
            return self._receive(uploads, display_name, request_id)

    def _receive(self, uploads, display_name, request_id):
        """接收阶段有界读取并取内容指纹；只有唯一提交可分配项目、保存文件。"""
        paths, fingerprint = set(), hashlib.sha256()
        fingerprint.update(json.dumps(display_name, ensure_ascii=False).encode())
        if not uploads:
            raise BusinessError("empty_project_files", "招标文件不能为空")
        for name, path, stream in uploads:
            normalized = normalize_relative_path(path)
            if normalized in paths:
                raise BusinessError(
                    "duplicate_relative_path",
                    "相对路径重复：" + normalized,
                )
            paths.add(normalized)
            if PurePosixPath(name).suffix.lower() not in UPLOAD_EXTENSIONS:
                raise BusinessError(
                    "unsupported_file_type",
                    "不支持此文件格式：" + name,
                )
            digest, size = hashlib.sha256(), 0
            stream.seek(0)
            while data := stream.read(1024 * 1024):
                size += len(data)
                if size > self.settings.storage.max_upload_bytes:
                    raise BusinessError("file_too_large", "单份文件超过上传限制")
                digest.update(data)
            stream.seek(0)
            if not size:
                raise BusinessError(
                    "empty_file", "不能上传空文件：" + name, http_status=422
                )
            fingerprint.update(
                json.dumps(
                    [name, normalized, size, digest.hexdigest()], ensure_ascii=False
                ).encode()
            )
        digest = fingerprint.hexdigest()
        with open_db() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT * FROM project_uploads WHERE request_id=?", (request_id,)
            ).fetchone()
            if prior:
                if prior["fingerprint"] != digest:
                    raise BusinessError(
                        "upload_request_conflict",
                        "此提交标识已用于不同文件或名称",
                        http_status=409,
                    )
                if prior["state"] == "failed" and not prior["project_id"]:
                    error = (
                        json.loads(prior["error_json"])
                        if prior["error_json"]
                        else {
                            "code": "process_interrupted",
                            "message": "接收已中断，尚未创建项目，请明确发起新的提交",
                            "http_status": 500,
                        }
                    )
                    raise BusinessError(**error)
                if prior["state"] == "receiving" or not prior["project_id"]:
                    raise BusinessError(
                        "upload_in_progress",
                        "同一次上传正在接收，请查询结果",
                        http_status=409,
                    )
                return self.creation_response(prior["project_id"], request_id), False
            # 先处理同一提交的幂等返回，再查名称，避免重试被自己的名称挡住。
            ensure_library_name_available(db, "project", display_name)
            db.execute(
                "INSERT INTO project_uploads(request_id,fingerprint,project_id,state) VALUES (?,?,NULL,'receiving')",
                (request_id, digest),
            )
        project = None
        try:
            project = self.projects.create()
            with open_db() as db:
                db.execute(
                    "UPDATE project_uploads SET project_id=? WHERE request_id=?",
                    (project.project_id, request_id),
                )
                if display_name is None:
                    display_name = default_library_name(
                        db, "project", project.project_id
                    )
            update_management(
                project.project_id,
                display_name=display_name or project.project_id,
                phase="received",
                client_request_id=request_id,
                trace_status="recording",
            )
            for name, path, stream in uploads:
                self.store.save(
                    owner=FileOwner(project_id=project.project_id),
                    data_role="rule_source",
                    stream=stream,
                    file_name=name,
                    relative_path=path,
                )
            with open_db() as db:
                db.execute(
                    "UPDATE project_uploads SET state='accepted' WHERE request_id=?",
                    (request_id,),
                )
            return self.creation_response(project.project_id, request_id), True
        except BaseException as exc:
            error = {
                "code": exc.code
                if isinstance(exc, BusinessError)
                else "internal_server_error",
                "message": exc.message
                if isinstance(exc, BusinessError)
                else "后端处理失败，请查看服务日志",
                "http_status": exc.http_status
                if isinstance(exc, BusinessError)
                else 500,
                "details": exc.details if isinstance(exc, BusinessError) else None,
            }
            if project:
                self.projects.update_status(
                    project.project_id,
                    "rule_failed",
                    error_code=type(exc).__name__,
                    error_message=str(exc),
                )
                update_management(
                    project.project_id,
                    phase="failed",
                    failed_phase="received",
                    trace_status="unavailable",
                )
            with open_db() as db:
                db.execute(
                    "UPDATE project_uploads SET state='failed',error_json=? WHERE request_id=?",
                    (json.dumps(error, ensure_ascii=False), request_id),
                )
            raise

    def creation_response(self, project_id, request_id):
        return {
            "project_id": project_id,
            "status": self.projects.require(project_id).status,
            "display_name": management(project_id).get("display_name", project_id),
            "file_count": len(
                self.files.list_by_scope(project_id=project_id, data_role="rule_source")
            ),
            "client_request_id": request_id,
        }

    def snapshot(self, project):
        """坏快照不伪装成空库；处理中只读取已持久化的阶段快照。"""
        kind = "final" if project.status == "rule_ready" else "partial"
        path = None
        try:
            path = (
                self.store.resolve_relative(project.rule_set_path)
                if project.rule_set_path
                else self.settings.storage.data_root
                / "parsed"
                / "rules"
                / f"{project.project_id}.json"
            )
            if kind == "partial":
                path = path.with_name(f"{project.project_id}.progress.json")
                if not path.exists():
                    return None, "none", None, None, None
            payload = path.read_bytes()
            rules = (
                RuleSet.model_validate_json(payload)
                if kind == "final"
                else RuleExtractionProgress.model_validate_json(payload).rule_set
            )
            if rules.project_id != project.project_id:
                raise ValueError("规则归属不符")
            return (
                rules,
                kind,
                revision_of(payload) if kind == "final" else None,
                None,
                path,
            )
        except (OSError, ValueError, BusinessError):
            return (
                None,
                "none",
                None,
                {
                    "code": "rule_set_unavailable",
                    "message": "规则快照缺失或损坏；仍可查看来源文件",
                },
                path,
            )

    def detail(self, project_id):
        project = self.projects.require(project_id)
        meta = management(project_id)
        rules, kind, revision, warning, _ = self.snapshot(project)
        available = False
        if project.status == "rule_ready" and rules is not None:
            try:
                self.check_index(project_id)
                available = bool(rules.rules)
            except (OSError, ValueError, RuntimeError, KeyError):
                warning = {
                    "code": "tender_index_unavailable",
                    "message": "招标原文索引不可用；可维护和导出规则，暂不可用于审核",
                }
        with open_db() as db:
            row = db.execute(
                "SELECT created_at,updated_at FROM projects WHERE project_id=?",
                (project_id,),
            ).fetchone()
        error = (
            {
                "code": project.error_code,
                "message": display_value(project.error_message)[0],
            }
            if project.error_code
            else None
        )
        return {
            "project": {
                "project_id": project_id,
                "display_name": meta.get("display_name", project_id),
                "status": project.status,
                **dict(row),
                "error": error,
            },
            "snapshot_kind": kind,
            "revision": revision,
            "available_for_audit": available,
            "warning": warning,
            "rule_set": rules.model_dump(mode="json") if rules is not None else None,
            "files": [
                self.file_info(file)
                for file in self.files.list_by_scope(
                    project_id=project_id, data_role="rule_source"
                )
            ],
        }

    def check_index(self, project_id):
        """读接口校验索引但不反复载入向量矩阵；任一文件变化即失效。"""
        root = self.settings.storage.data_root / "indexes" / f"rules-{project_id}"
        fingerprint = embedding_config_fingerprint(self.settings.embedding_model)
        signature = (
            fingerprint,
            tuple(
                (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
                for name in (
                    "index.json",
                    "chunks.jsonl",
                    "embeddings.npy",
                    "keyword_index.json",
                )
                for stat in [(root / name).stat()]
            ),
        )
        with _index_check_lock:
            if _index_checks.get(str(root)) != signature:
                load_index(f"rules-{project_id}", expected_fingerprint=fingerprint)
                _index_checks[str(root)] = signature

    def file_info(self, file):
        return {
            "file_id": file.file_id,
            "file_name": file.file_name,
            "source_path": file.source_path,
            "extension": file.extension,
            "size_bytes": file.size_bytes,
            "parse_status": file.parse_status,
            "preview_available": (
                self.settings.storage.data_root / "parsed" / f"{file.file_id}.json"
            ).is_file(),
        }

    def list(
        self,
        *,
        page=1,
        page_size=12,
        q=None,
        status=None,
        attention_only=False,
        client_request_id=None,
    ):
        with open_db() as db:
            ids = db.execute(
                "SELECT project_id FROM projects ORDER BY updated_at DESC,project_id DESC"
            ).fetchall()
            requested = (
                db.execute(
                    "SELECT project_id FROM project_uploads WHERE request_id=?",
                    (client_request_id,),
                ).fetchone()
                if client_request_id
                else None
            )
        items = []
        overview = {
            "project_count": len(ids),
            "ready_count": 0,
            "processing_count": 0,
            "failed_count": 0,
            "attention_count": 0,
        }
        for row in ids:
            detail = self.detail(row["project_id"])
            project = detail["project"]
            rules = detail["rule_set"]
            attention = (
                project["status"] == "rule_failed" or detail["warning"] is not None
            )
            overview["ready_count"] += int(detail["available_for_audit"])
            overview["processing_count"] += int(project["status"] == "processing_rules")
            overview["failed_count"] += int(project["status"] == "rule_failed")
            overview["attention_count"] += int(attention)
            if client_request_id and (
                not requested or project["project_id"] != requested["project_id"]
            ):
                continue
            if (
                q
                and q.casefold()
                not in (
                    project["display_name"] + " " + project["project_id"]
                ).casefold()
            ):
                continue
            if (
                status
                and project["status"] != status
                or attention_only
                and not attention
            ):
                continue
            counts = {
                category: sum(r["category"] == category for r in rules["rules"])
                if rules
                else 0
                for category in CATEGORY_LABELS
            }
            corrupt = (
                detail["warning"]
                and detail["warning"]["code"] == "rule_set_unavailable"
            )
            items.append(
                {
                    **{k: v for k, v in project.items() if k != "error"},
                    "phase": management(project["project_id"]).get("phase"),
                    "file_count": len(detail["files"]),
                    "rule_count": None
                    if corrupt
                    else len(rules["rules"])
                    if rules
                    else 0,
                    "category_counts": None if corrupt else counts,
                    **{
                        k: detail[k]
                        for k in ("snapshot_kind", "available_for_audit", "warning")
                    },
                }
            )
        return {
            "items": items[(page - 1) * page_size : page * page_size],
            "total": len(items),
            "page": page,
            "page_size": page_size,
            "overview": overview,
        }

    def progress(self, project_id, include_trace=False, after_seq=0, limit=100):
        project = self.projects.require(project_id)
        meta = management(project_id)
        rules, kind, _, warning, _ = self.snapshot(project)
        saved = None
        try:
            saved = RuleExtractionProgress.model_validate_json(
                (
                    self.settings.storage.data_root
                    / "parsed"
                    / "rules"
                    / f"{project_id}.progress.json"
                ).read_bytes()
            )
        except (OSError, ValueError):
            pass
        completed = saved.completed_file_ids if saved else []
        current = meta.get("current_file_id") or (
            saved.current_file_id if saved and project.status != "rule_ready" else None
        )
        recent_ids = (
            list(
                dict.fromkeys(
                    r.rule_id
                    for c in reversed(saved.commits)
                    for r in reversed(c.after)
                )
            )
            if saved
            else []
        )
        by_id = {r.rule_id: r for r in rules.rules} if rules else {}
        recent = [
            by_id[rid].model_dump(mode="json") for rid in recent_ids if rid in by_id
        ][:3]
        files = []
        for file in self.files.list_by_scope(
            project_id=project_id, data_role="rule_source"
        ):
            extraction = "completed" if file.file_id in completed else "pending"
            if (
                file.file_id == current
                and file.file_id not in completed
                and (
                    meta.get("phase") == "extracting"
                    or meta.get("failed_phase") == "extracting"
                )
            ):
                extraction = (
                    "failed" if project.status == "rule_failed" else "processing"
                )
            files.append({**self.file_info(file), "extraction_status": extraction})
        return {
            "project_id": project_id,
            "status": project.status,
            "phase": meta.get("phase", "received"),
            "display_name": meta.get("display_name", project_id),
            "failed_phase": meta.get("failed_phase"),
            "started_at": meta.get("started_at"),
            "updated_at": meta.get("updated_at"),
            "current_file_id": current,
            "document_count": len(files),
            "completed_file_ids": completed,
            "saved_rule_count": len(by_id),
            "snapshot_kind": kind,
            "can_resume": bool(
                project.status == "rule_failed"
                and meta.get("failed_phase") in {"extracting", "saving"}
                and saved is not None
                and saved.project_id == project_id
                and warning is None
            ),
            "files": files,
            "recent_rules": recent,
            # 只读展示索引，来自完整快照；不改变真实规则 ID 或持久化结构。
            "rule_order": list(by_id),
            "trace": self.trace_page(project_id, project.status, after_seq, limit)
            if include_trace
            else None,
            "error": {
                "code": project.error_code,
                "message": display_value(project.error_message)[0],
                "file_id": current,
            }
            if project.error_code
            else warning,
        }

    def claim_resume(self, project_id):
        """校验后原子占用失败任务，防止重复点击/并发启动。"""
        from app.services.project_service import ProjectService

        project = self.projects.require(project_id)
        meta = management(project_id)
        if project.status != "rule_failed":
            raise BusinessError(
                "rule_resume_conflict",
                "仅失败的规则库可以续跑；任务可能已经启动",
                http_status=409,
            )
        if meta.get("failed_phase") not in {"extracting", "saving"}:
            raise BusinessError(
                "rule_resume_unavailable",
                "尚未完成解析和索引，没有可续跑的规则进度",
                http_status=409,
            )
        try:
            ProjectService(self.settings).load_resume(project_id)
            self.check_index(project_id)
        except (OSError, ValueError, RuntimeError, KeyError, BusinessError) as exc:
            raise BusinessError(
                "rule_resume_unavailable",
                "无法安全续跑：进度或原解析/索引快照缺失、不一致，或旧进度没有失败文件的起点快照；已保存成果未改动",
                http_status=409,
            ) from exc
        with open_db() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE projects SET status='processing_rules',error_code=NULL,"
                "error_message=NULL,updated_at=? WHERE project_id=? AND status='rule_failed'",
                (utc_now(), project_id),
            ).rowcount
            if changed != 1:
                raise BusinessError(
                    "rule_resume_conflict", "任务已被启动，请刷新进度", http_status=409
                )
            meta.update(
                phase="extracting", failed_phase=None, trace_status="recording",
                resumed_at=utc_now(), updated_at=utc_now(),
            )
            db.execute(
                "UPDATE project_management SET payload=? WHERE project_id=?",
                (json.dumps(meta, ensure_ascii=False), project_id),
            )
        return {"project_id": project_id, "status": "processing_rules"}

    def trace_page(self, project_id, status, after_seq, limit):
        try:
            with open_db() as db:
                rows = db.execute(
                    "SELECT seq,node FROM project_trace WHERE project_id=? AND seq>? ORDER BY seq LIMIT ?",
                    (project_id, after_seq, limit + 1),
                ).fetchall()
            changes = [
                {"seq": row["seq"], "node": json.loads(row["node"])}
                for row in rows[:limit]
            ]
            trace_status = management(project_id).get("trace_status", "unavailable")
            return {
                "trace_status": "recording"
                if status == "processing_rules"
                else trace_status,
                "changes": changes,
                "next_cursor": changes[-1]["seq"] if changes else after_seq,
                "has_more": len(rows) > limit,
                "warning": "部分轨迹未记录"
                if trace_status in {"partial", "unavailable"}
                else None,
            }
        except Exception:
            logger.warning("读取规则库轨迹失败 project=%s", project_id, exc_info=True)
            return {
                "trace_status": "recording"
                if status == "processing_rules"
                else "unavailable",
                "changes": [],
                "next_cursor": after_seq,
                "has_more": False,
                "warning": "轨迹暂不可读取",
            }

    def trace_detail(self, project_id, span_id):
        self.projects.require(project_id)
        with open_db() as db:
            row = db.execute(
                "SELECT * FROM project_trace WHERE project_id=? AND span_id=? ORDER BY seq DESC LIMIT 1",
                (project_id, span_id),
            ).fetchone()
        if not row:
            raise NotFoundError("trace_not_found", "执行节点不存在")
        return {
            "project_id": project_id,
            "seq": row["seq"],
            "node": json.loads(row["node"]),
            **json.loads(row["detail"]),
        }

    def edit(self, project_id, request, if_match):
        if if_match is None:
            raise BusinessError(
                "revision_required", "缺少 If-Match 版本", http_status=428
            )
        with _locks[project_id]:
            project = self.projects.require(project_id)
            if project.status != "rule_ready":
                raise BusinessError(
                    "rule_set_not_ready", "阶段结果只读", http_status=409
                )
            rules, _, revision, warning, path = self.snapshot(project)
            if rules is None:
                raise BusinessError(
                    "rule_set_unavailable", warning["message"], http_status=500
                )
            if if_match != f'"{revision}"':
                raise BusinessError(
                    "rule_set_revision_conflict",
                    "规则库已变化，请重新载入后比较；草稿未保存",
                    http_status=409,
                )
            original = rules.model_dump(mode="json")
            data = request.data
            if request.action == "update_profile":
                rules.project_profile = data.project_profile
                result = {"project_profile": rules.project_profile}
            else:
                if normalize_priority_verification(
                    data.priority, data.verification
                ) != (data.priority, data.verification):
                    raise BusinessError(
                        "invalid_rule_policy_combination",
                        "资料核验规则至少选择重要",
                        http_status=422,
                    )
                owned = {
                    f.file_id: f
                    for f in self.files.list_by_scope(
                        project_id=project_id, data_role="rule_source"
                    )
                }
                if (
                    len(set(data.source_file_ids)) != len(data.source_file_ids)
                    or not set(data.source_file_ids) <= owned.keys()
                ):
                    raise BusinessError(
                        "invalid_rule_sources",
                        "来源必须为当前项目不重复的文件",
                        http_status=422,
                    )
                sources = [
                    RuleSourceFile(
                        file_id=fid,
                        file_name=owned[fid].file_name,
                        source_path=owned[fid].source_path,
                    )
                    for fid in data.source_file_ids
                ]
                old = None
                if request.action == "update_rule":
                    old = next(
                        (r for r in rules.rules if r.rule_id == data.rule_id), None
                    )
                    if old is None:
                        raise NotFoundError("rule_not_found", "规则不存在")
                    rid = old.rule_id
                else:
                    # 只分配新 ID，不重编号已有规则。
                    rid = next_id("rule")
                    while any(r.rule_id == rid for r in rules.rules):
                        rid = next_id("rule")
                rule = RuleItem(
                    rule_id=rid,
                    provenance=old.provenance if old else "human_added",
                    sources=sources,
                    **data.model_dump(exclude={"source_file_ids", "rule_id"}),
                )
                if old and rule != old and old.provenance == "ai_generated":
                    rule.provenance = "human_modified"
                rules.rules = (
                    [rule if r.rule_id == rid else r for r in rules.rules]
                    if old
                    else [*rules.rules, rule]
                )
                result = {"rule": rule.model_dump(mode="json")}
            checked = RuleSet.model_validate(rules.model_dump())
            if checked.model_dump(mode="json") != original:
                payload = checked.model_dump_json(indent=2).encode()
                atomic_write(path, payload)
                revision = revision_of(payload)
                with open_db() as db:
                    db.execute(
                        "UPDATE projects SET updated_at=? WHERE project_id=?",
                        (utc_now(), project_id),
                    )
            return {**result, "revision": revision}

    def owned_file(self, project_id, file_id):
        self.projects.require(project_id)
        file = self.files.require(file_id)
        if file.project_id != project_id or file.data_role != "rule_source":
            raise NotFoundError("file_not_found", "当前规则库没有此来源文件")
        return file

    def parsed(self, project_id, file_id, version=None):
        return SourcePreviewService(self.settings, self.store).parsed(
            self.owned_file(project_id, file_id), version
        )

    def preview(self, project_id, file_id):
        return SourcePreviewService(self.settings, self.store).preview(
            self.owned_file(project_id, file_id),
            f"/api/projects/{project_id}/files/{file_id}/preview",
        )

    def image(self, project_id, file_id, image_name, version):
        return SourcePreviewService(self.settings, self.store).image(
            self.owned_file(project_id, file_id), image_name, version
        )

    def export(self, project_id, format):
        project = self.projects.require(project_id)
        if project.status != "rule_ready":
            raise BusinessError(
                "rule_set_not_ready", "只有正式规则库可导出", http_status=409
            )
        rules, _, revision, warning, _ = self.snapshot(project)
        if rules is None:
            raise BusinessError(
                "rule_set_unavailable", warning["message"], http_status=500
            )
        if format == "json":
            content = rules.model_dump_json(indent=2)
        else:
            content = (
                f"# {management(project_id).get('display_name', project_id)}\n\n"
                + build_rule_view(rules)
            )
            content += "\n\n## 来源文件\n\n" + "\n".join(
                f"- {s.file_id}：{s.file_name}（{s.source_path}）"
                for s in rules.source_files
            )
        return content.encode(), revision
