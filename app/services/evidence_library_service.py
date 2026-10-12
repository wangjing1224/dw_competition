"""资料库管理读写：读取不建模型；内容以原子保存的 EvidenceLibrary 为准。"""

import fcntl
import hashlib
import json
import logging
import re
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import PurePosixPath

from app.audit.context import fixed_parsed, verify_indexes
from app.core.config import get_settings
from app.core.errors import BusinessError, NotFoundError
from app.evidence.storage import (
    library_path,
    load_library,
    progress_path,
    write_library,
)
from app.models.evidence import (
    EvidenceIngestionProgress,
    EvidenceLibrary,
    EvidenceMaterial,
    EvidenceSourceFile,
)
from app.retrieval.index import embedding_config_fingerprint
from app.retrieval.store import CorruptedIndex, IndexRebuildRequired
from app.rules.local_trace import display_value
from app.services.rule_library_service import UPLOAD_EXTENSIONS, revision_of
from app.services.source_preview import SourcePreviewService
from app.storage.bidder_management import management, update_management
from app.storage.database import open_db
from app.storage.file_store import FileOwner, FileStore, normalize_relative_path
from app.storage.library_names import (
    ensure_library_name_available,
    lock_library_names,
)
from app.storage.repositories import BidderRepository, FileRepository, utc_now

logger = logging.getLogger(__name__)
PROCESSING = {"uploading_evidence", "processing_evidence"}
_index_checks = {}
_index_lock = threading.Lock()


class EvidenceLibraryService:
    def __init__(self):
        self.settings = get_settings()
        self.bidders, self.files, self.store = (
            BidderRepository(),
            FileRepository(),
            FileStore(),
        )
        self.sources = SourcePreviewService(self.settings, self.store)

    def receive(self, uploads, name, request_id):
        with lock_library_names("bidder"):
            return self._receive(uploads, name, request_id)

    def _receive(self, uploads, name, request_id):
        # 请求内读完并复位流；后台只接收 ID，不持有 UploadFile。
        seen, fingerprint = set(), hashlib.sha256(name.encode())
        for filename, path, stream in uploads:
            normalized = normalize_relative_path(path)
            if normalized in seen:
                raise BusinessError(
                    "duplicate_relative_path", "同批文件相对路径重复", http_status=422
                )
            seen.add(normalized)
            if PurePosixPath(filename).suffix.lower() not in UPLOAD_EXTENSIONS:
                raise BusinessError(
                    "unsupported_file_type", "不支持的文件类型", http_status=422
                )
            digest, size = hashlib.sha256(), 0
            stream.seek(0)
            while data := stream.read(1024 * 1024):
                size += len(data)
                if size > self.settings.storage.max_upload_bytes:
                    raise BusinessError(
                        "file_too_large", "单份文件超过上传限制", http_status=413
                    )
                digest.update(data)
            stream.seek(0)
            if not size:
                raise BusinessError("empty_file", "不能上传空文件", http_status=422)
            fingerprint.update(
                json.dumps([filename, normalized, size, digest.hexdigest()]).encode()
            )
        if not seen:
            raise BusinessError(
                "empty_evidence_files", "投标方资料不能为空", http_status=422
            )
        with open_db() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT * FROM bidder_uploads WHERE request_id=?", (request_id,)
            ).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint.hexdigest():
                    raise BusinessError(
                        "upload_request_conflict",
                        "此提交标识已用于不同内容",
                        http_status=409,
                    )
                if prior["state"] == "receiving":
                    raise BusinessError(
                        "upload_in_progress",
                        "同一次上传正在接收，请查询结果",
                        http_status=409,
                    )
                if not prior["bidder_id"]:
                    if prior["error_json"]:
                        raise BusinessError(**json.loads(prior["error_json"]))
                    raise BusinessError(
                        "upload_failed",
                        "此提交接收失败，请明确发起新提交",
                        http_status=409,
                    )
                return self.creation_response(
                    prior["bidder_id"], request_id, True
                ), False
            ensure_library_name_available(db, "bidder", name)
            db.execute(
                "INSERT INTO bidder_uploads VALUES (?,?,NULL,'receiving',NULL)",
                (request_id, fingerprint.hexdigest()),
            )
        bidder = None
        try:
            bidder = self.bidders.create(status="uploading_evidence")
            with open_db() as db:
                db.execute(
                    "UPDATE bidder_uploads SET bidder_id=? WHERE request_id=?",
                    (bidder.bidder_id, request_id),
                )
            update_management(
                bidder.bidder_id,
                display_name=name,
                phase="received",
                trace_status="recording",
                client_request_id=request_id,
            )
            for filename, path, stream in uploads:
                self.store.save(
                    owner=FileOwner(bidder_id=bidder.bidder_id),
                    data_role="evidence_material",
                    stream=stream,
                    file_name=filename,
                    relative_path=path,
                )
            self.bidders.update_status(bidder.bidder_id, "processing_evidence")
            with open_db() as db:
                db.execute(
                    "UPDATE bidder_uploads SET state='accepted' WHERE request_id=?",
                    (request_id,),
                )
            return self.creation_response(bidder.bidder_id, request_id, False), True
        except BaseException as exc:
            # 接收失败同样保留提交标识；重复请求只返回结果，不再次创建或排队。
            error = {
                "code": exc.code if isinstance(exc, BusinessError) else "upload_failed",
                "message": exc.message
                if isinstance(exc, BusinessError)
                else "原件接收失败，请查看服务日志；重新上传需明确发起新提交",
                "http_status": exc.http_status
                if isinstance(exc, BusinessError)
                else 500,
            }
            with open_db() as db:
                db.execute(
                    "UPDATE bidder_uploads SET state='failed',error_json=? WHERE request_id=?",
                    (json.dumps(error, ensure_ascii=False), request_id),
                )
            if bidder:
                self.bidders.update_status(
                    bidder.bidder_id,
                    "evidence_failed",
                    error_code="upload_incomplete",
                    error_message="原件接收失败，已保存文件保留；没有启动入库",
                )
                update_management(
                    bidder.bidder_id,
                    phase="failed",
                    failed_phase="received",
                    trace_status="unavailable",
                )
            raise

    def creation_response(self, bidder_id, request_id, reused):
        return {
            "bidder_id": bidder_id,
            "status": self.bidders.require(bidder_id).status,
            "client_request_id": request_id,
            "reused": reused,
        }

    def records(self, bidder_id):
        return self.files.list_by_scope(
            bidder_id=bidder_id, data_role="evidence_material"
        )

    def snapshot(self, bidder):
        try:
            library = load_library(bidder.bidder_id, self.settings)
            records = self.records(bidder.bidder_id)
            if library:
                actual = [(f.file_id, f.file_name, f.source_path) for f in records]
                declared = [
                    (f.file_id, f.file_name, f.source_path)
                    for f in library.source_files
                ]
                if actual != declared:
                    raise ValueError("来源清单与上传登记不符")
            if bidder.status == "evidence_ready" and (
                library is None
                or not records
                or {m.file_id for m in library.materials}
                != {f.file_id for f in records}
            ):
                raise ValueError("完成库缺少资料")
            kind = (
                "final"
                if library and bidder.status == "evidence_ready"
                else "partial"
                if library
                else "none"
            )
            return library, kind, None
        except (OSError, ValueError):
            return (
                None,
                "none",
                {
                    "code": "evidence_library_unavailable",
                    "message": "资料快照缺失、损坏或与登记清单不符；原件仍可下载",
                },
            )

    def revision(self, library, name):
        return revision_of((name + "\n" + library.model_dump_json()).encode())

    def saved_progress(self, bidder_id):
        path = progress_path(bidder_id, self.settings)
        if not path.exists():
            return None, None
        try:
            progress = EvidenceIngestionProgress.model_validate_json(path.read_bytes())
            ids = [f.file_id for f in self.records(bidder_id)]
            if (
                progress.bidder_id != bidder_id
                or progress.file_ids != ids
                or not set(progress.completed_file_ids) <= set(ids)
            ):
                raise ValueError("进度归属错误")
            return progress, None
        except (OSError, ValueError):
            return None, {"code": "progress_unavailable", "message": "执行进度不可读取"}

    def file_views(self, bidder, library, progress):
        known = {m.file_id: m for m in library.materials} if library else {}
        complete = set(progress.completed_file_ids) if progress else set()
        if bidder.status == "evidence_ready" and library:
            complete = set(known)
        current = (
            progress.current_file_id
            if progress
            else management(bidder.bidder_id).get("current_file_id")
        )
        result = []
        for file in self.records(bidder.bidder_id):
            limit = None
            if file.parse_status != "pending":
                try:
                    _, parsed = self.sources.parsed(file)
                    limit = (
                        bool(parsed.warnings)
                        or parsed.parse_status != "parsed"
                        or any(
                            image.read_status in {"partial", "unreadable", "failed"}
                            for section in parsed.sections
                            for block in section.blocks
                            for image in block.images
                        )
                    )
                except BusinessError:
                    limit = True
            status = "completed" if file.file_id in complete else "pending"
            if file.file_id == current and status != "completed":
                status = (
                    "failed" if bidder.status == "evidence_failed" else "processing"
                )
            result.append(
                {
                    "file_id": file.file_id,
                    "file_name": file.file_name,
                    "source_path": file.source_path,
                    "extension": file.extension,
                    "size_bytes": file.size_bytes,
                    "parse_status": file.parse_status,
                    "ingestion_status": status,
                    "has_material": file.file_id in known,
                    "has_reading_limit": limit,
                    "provenance": known[file.file_id].provenance
                    if file.file_id in known
                    else None,
                }
            )
        return result

    def detail(self, bidder_id):
        bidder = self.bidders.require(bidder_id)
        meta = management(bidder_id)
        library, kind, warning = self.snapshot(bidder)
        progress, progress_warning = self.saved_progress(bidder_id)
        warning = warning or progress_warning
        editable = kind == "final"
        available = False
        if editable:
            try:
                self.check_index(library)
                available = True
            except (
                OSError,
                ValueError,
                BusinessError,
                CorruptedIndex,
                IndexRebuildRequired,
            ):
                warning = warning or {
                    "code": "evidence_index_unavailable",
                    "message": "资料或索引完整性检查未通过，暂不可用于审核；合法摘要仍可维护、导出",
                }
        with open_db() as db:
            row = db.execute(
                "SELECT created_at,updated_at FROM bidders WHERE bidder_id=?",
                (bidder_id,),
            ).fetchone()
        name = meta.get("display_name", bidder_id)
        times = dict(row)
        times["updated_at"] = self.updated_at(bidder_id, times["updated_at"], meta)
        return {
            "bidder": {
                "bidder_id": bidder_id,
                "display_name": name,
                "status": bidder.status,
                **times,
            },
            "library": library.model_dump(mode="json") if library else None,
            "snapshot_kind": kind,
            "revision": self.revision(library, name) if editable else None,
            "editable": editable,
            "exportable": editable,
            "available_for_audit": available,
            "files": self.file_views(bidder, library, progress),
            "warning": warning,
            "error": {
                "code": bidder.error_code,
                "message": display_value(bidder.error_message)[0],
            }
            if bidder.error_code
            else None,
        }

    def updated_at(self, bidder_id, fallback, meta):
        """内容原子落盘时间也参与展示；管理时间写失败不会掩盖已提交的修订。"""
        times = [fallback or "", meta.get("updated_at", "")]
        path = library_path(bidder_id, self.settings)
        try:
            times.append(datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat())
        except FileNotFoundError:
            pass
        return max(times)

    def check_index(self, library):
        """只在解析或索引/配置变化后重做完整校验，轮询不反复读取向量矩阵。"""
        root = self.settings.storage.data_root
        index = root / "indexes" / f"evidence-{library.bidder_id}"
        paths = [
            index / name
            for name in (
                "index.json",
                "chunks.jsonl",
                "embeddings.npy",
                "keyword_index.json",
            )
        ]
        paths += [
            root / "parsed" / f"{source.file_id}.json"
            for source in library.source_files
        ]
        signature = (
            embedding_config_fingerprint(self.settings.embedding_model),
            self.settings.retrieval.chunk_tokens,
            self.settings.retrieval.chunk_overlap,
            tuple(
                (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
                for path in paths
                for stat in [path.stat()]
            ),
        )
        key = str(index)
        with _index_lock:
            if _index_checks.get(key) != signature:
                parsed = fixed_parsed(
                    library.source_files,
                    self.files,
                    role="evidence_material",
                    bidder_id=library.bidder_id,
                )
                verify_indexes(parsed, self.settings)
                _index_checks[key] = signature

    def list(
        self,
        *,
        page,
        page_size,
        q=None,
        status=None,
        attention_only=False,
        client_request_id=None,
    ):
        if client_request_id and (q or status or attention_only):
            raise BusinessError(
                "invalid_filters", "恢复提交查询不能混用其他筛选条件", http_status=422
            )
        with open_db() as db:
            ids = [r[0] for r in db.execute("SELECT bidder_id FROM bidders")]
            lookup = (
                db.execute(
                    "SELECT bidder_id FROM bidder_uploads WHERE request_id=?",
                    (client_request_id,),
                ).fetchone()
                if client_request_id
                else None
            )
        items, overview = (
            [],
            {
                "library_count": len(ids),
                "ready_count": 0,
                "processing_count": 0,
                "attention_count": 0,
            },
        )
        for bidder_id in ids:
            detail = self.detail(bidder_id)
            base, library = detail["bidder"], detail["library"]
            attention = base["status"] == "evidence_failed" or bool(detail["warning"])
            overview["ready_count"] += int(detail["available_for_audit"])
            overview["processing_count"] += int(base["status"] in PROCESSING)
            overview["attention_count"] += int(attention)
            if client_request_id and (not lookup or bidder_id != lookup[0]):
                continue
            if (
                q
                and q.casefold()
                not in (base["display_name"] + " " + bidder_id).casefold()
            ):
                continue
            if status and not (
                base["status"] in PROCESSING
                if status == "processing_evidence"
                else base["status"] == status
            ):
                continue
            if attention_only and not attention:
                continue
            limits = [
                f["has_reading_limit"]
                for f in detail["files"]
                if f["has_reading_limit"] is not None
            ]
            corrupt = (
                detail["warning"]
                and detail["warning"]["code"] == "evidence_library_unavailable"
            )
            items.append(
                {
                    **base,
                    "file_count": len(detail["files"]),
                    "material_count": None
                    if corrupt
                    else len(library["materials"])
                    if library
                    else 0,
                    "available_for_audit": detail["available_for_audit"],
                    "has_reading_limit": any(limits) if limits else None,
                    "warning": detail["warning"],
                }
            )
        items.sort(
            key=lambda item: (item["updated_at"], item["bidder_id"]), reverse=True
        )
        return {
            "items": items[(page - 1) * page_size : page * page_size],
            "total": len(items),
            "page": page,
            "page_size": page_size,
            "overview": overview,
        }

    def progress(self, bidder_id, include_trace=False, after_seq=0, limit=100):
        bidder = self.bidders.require(bidder_id)
        meta = management(bidder_id)
        library, kind, warning = self.snapshot(bidder)
        saved, progress_warning = self.saved_progress(bidder_id)
        known = {m.file_id: m for m in library.materials} if library else {}
        recent = [
            known[fid] for fid in reversed(meta.get("saved_order", [])) if fid in known
        ][:3]
        return {
            "bidder_id": bidder_id,
            "display_name": meta.get("display_name", bidder_id),
            "status": bidder.status,
            "phase": meta.get("phase", "received"),
            "failed_phase": meta.get("failed_phase"),
            "started_at": meta.get("started_at"),
            "updated_at": meta.get("updated_at"),
            "current_file_id": None
            if bidder.status == "evidence_ready"
            else saved.current_file_id
            if saved
            else meta.get("current_file_id"),
            "document_count": len(self.records(bidder_id)),
            "completed_file_ids": saved.completed_file_ids if saved else [],
            "saved_material_count": None if warning else len(known),
            "snapshot_kind": kind,
            "files": self.file_views(bidder, library, saved),
            "recent_materials": [
                {"file_id": m.file_id, "bid_summary": m.bid_summary} for m in recent
            ],
            "trace": self.trace_page(bidder_id, after_seq, limit)
            if include_trace
            else None,
            "warning": warning or progress_warning,
            "can_resume": bool(
                bidder.status == "evidence_failed"
                and bidder.error_code != "upload_incomplete"
                and meta.get("failed_phase") == "ingesting"
                and saved is not None
                and library is not None
                and not (warning or progress_warning)
            ),
            "error": {
                "code": bidder.error_code,
                "message": display_value(bidder.error_message)[0],
            }
            if bidder.error_code
            else None,
        }

    def claim_resume(self, bidder_id):
        """只读检查恢复依据，再原子占用任务；不接收新文件、不调用模型。"""
        bidder = self.bidders.require(bidder_id)
        if bidder.status != "evidence_failed":
            raise BusinessError(
                "evidence_resume_conflict",
                "仅失败的资料库可重试，请刷新状态",
                http_status=409,
            )
        meta = management(bidder_id)
        if (
            bidder.error_code == "upload_incomplete"
            or meta.get("failed_phase") != "ingesting"
        ):
            raise BusinessError(
                "evidence_resume_unavailable",
                "尚无可恢复的资料整理进度，请重新上传",
                http_status=409,
            )
        try:
            saved, warning = self.saved_progress(bidder_id)
            library = load_library(bidder_id, self.settings)
            if saved is None or warning or library is None:
                raise ValueError("资料或进度缺失")
            records = self.records(bidder_id)
            sources, versions = [], {}
            for record in records:
                _, parsed = self.sources.parsed(record)
                if parsed.source_sha256 != record.sha256:
                    raise ValueError("原文件与解析版本不一致")
                sources.append(
                    EvidenceSourceFile(
                        file_id=record.file_id,
                        file_name=record.file_name,
                        source_path=record.source_path,
                    )
                )
                versions[record.file_id] = hashlib.sha256(
                    parsed.model_dump_json().encode()
                ).hexdigest()
            if (
                library.source_files != sources
                or saved.parser_versions != versions
                or saved.completed_file_ids
                != saved.file_ids[: len(saved.completed_file_ids)]
                or not set(saved.completed_file_ids)
                <= {m.file_id for m in library.materials}
            ):
                raise ValueError("资料清单、解析版本或完成进度不一致")
            self.check_index(library)
        except (OSError, ValueError, RuntimeError, KeyError, BusinessError) as exc:
            raise BusinessError(
                "evidence_resume_unavailable",
                "恢复所需资料、进度或索引不完整或已变化，不能安全重试；已保存资料保留",
                http_status=409,
            ) from exc
        with open_db() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE bidders SET status='processing_evidence', error_code=NULL, "
                "error_message=NULL, updated_at=? WHERE bidder_id=? AND status='evidence_failed'",
                (utc_now(), bidder_id),
            ).rowcount
            if changed != 1:
                raise BusinessError(
                    "evidence_resume_conflict",
                    "资料库已在处理，请勿重复重试",
                    http_status=409,
                )
            meta.update(
                phase="ingesting", failed_phase=None, trace_status="recording",
                resumed_at=utc_now(), updated_at=utc_now(),
            )
            db.execute(
                "UPDATE bidder_management SET payload=? WHERE bidder_id=?",
                (json.dumps(meta, ensure_ascii=False), bidder_id),
            )
        return {"bidder_id": bidder_id, "status": "processing_evidence"}

    def trace_page(self, bidder_id, after_seq, limit):
        try:
            with open_db() as db:
                rows = db.execute(
                    "SELECT seq,node FROM bidder_trace WHERE bidder_id=? AND seq>? ORDER BY seq LIMIT ?",
                    (bidder_id, after_seq, limit + 1),
                ).fetchall()
            changes = [
                {"seq": r["seq"], "node": json.loads(r["node"])} for r in rows[:limit]
            ]
            status = management(bidder_id).get("trace_status", "unavailable")
            return {
                "trace_status": status,
                "changes": changes,
                "next_cursor": changes[-1]["seq"] if changes else after_seq,
                "has_more": len(rows) > limit,
                "warning": "部分轨迹未记录"
                if status in {"partial", "unavailable"}
                else None,
            }
        except Exception:
            logger.warning("读取资料轨迹失败 bidder=%s", bidder_id, exc_info=True)
            return {
                "trace_status": "unavailable",
                "changes": [],
                "next_cursor": after_seq,
                "has_more": False,
                "warning": "轨迹暂不可读取",
            }

    def trace_detail(self, bidder_id, span_id):
        self.bidders.require(bidder_id)
        with open_db() as db:
            row = db.execute(
                "SELECT * FROM bidder_trace WHERE bidder_id=? AND span_id=? ORDER BY seq DESC LIMIT 1",
                (bidder_id, span_id),
            ).fetchone()
        if not row:
            raise NotFoundError("trace_not_found", "当前资料库没有此节点")
        return {
            "bidder_id": bidder_id,
            "seq": row["seq"],
            "node": json.loads(row["node"]),
            "material_change": None,
            **json.loads(row["detail"]),
        }

    @contextmanager
    def lock(self, bidder_id):
        self.bidders.require(bidder_id)
        path = library_path(bidder_id, self.settings).with_suffix(".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BusinessError(
                    "library_busy", "资料库正在处理或保存，请稍后重试", http_status=409
                ) from exc
            yield

    def editable_snapshot(self, bidder_id):
        bidder = self.bidders.require(bidder_id)
        if bidder.status != "evidence_ready":
            raise BusinessError(
                "evidence_library_not_ready", "阶段资料只读", http_status=409
            )
        library, _, warning = self.snapshot(bidder)
        if library is None:
            raise BusinessError(
                "evidence_library_unavailable", warning["message"], http_status=500
            )
        return library

    def edit(self, bidder_id, request, if_match):
        if if_match is None:
            raise BusinessError(
                "revision_required", "请提供资料库版本", http_status=428
            )
        if not re.fullmatch(r'"sha256:[a-f0-9]{64}"', if_match):
            raise BusinessError(
                "invalid_revision", "If-Match版本格式错误", http_status=422
            )
        with self.lock(bidder_id):
            library = self.editable_snapshot(bidder_id)
            meta = management(bidder_id)
            name = meta.get("display_name", bidder_id)
            revision = self.revision(library, name)
            if if_match != f'"{revision}"':
                raise BusinessError(
                    "evidence_revision_conflict",
                    "资料库已变化，请载入最新内容比较；草稿未保存",
                    http_status=409,
                )
            changed, file_id = False, None
            if request.action == "rename":
                changed = name != request.data.display_name
                if changed:
                    name = request.data.display_name
                    with lock_library_names("bidder"):
                        with open_db() as db:
                            ensure_library_name_available(
                                db, "bidder", name, exclude_id=bidder_id
                            )
                        meta = update_management(bidder_id, display_name=name)
            else:
                file_id = request.data.file_id
                old = next((m for m in library.materials if m.file_id == file_id), None)
                if old is None:
                    raise NotFoundError("material_not_found", "只能修订已有资料")
                payload = request.data.model_dump()
                payload["relations"] = list(dict.fromkeys(request.data.relations))
                material = EvidenceMaterial(**payload, provenance=old.provenance)
                changed = old != material
                if changed:
                    self.validate_images(
                        bidder_id,
                        material.bid_summary + "\n" + material.evidence_summary,
                    )
                    material.provenance = "human_modified"
                    try:
                        library = EvidenceLibrary(
                            bidder_id=bidder_id,
                            source_files=library.source_files,
                            materials=[
                                material if m.file_id == file_id else m
                                for m in library.materials
                            ],
                        )
                    except ValueError as exc:
                        raise BusinessError(
                            "invalid_material_relations",
                            "关系只能指向本库其他已有资料",
                            http_status=422,
                        ) from exc
                    write_library(library, self.settings)
            # JSON已提交后管理时间失败不能响应“内容未保存”；展示时间以原子落盘时间兜底。
            warning = None
            if changed and request.action != "rename":
                try:
                    meta = update_management(bidder_id)
                except Exception:
                    logger.exception(
                        "资料已保存，管理时间使用落盘时间 bidder=%s", bidder_id
                    )
                    warning = "内容已保存，管理更新时间暂以内容落盘时间为准"
            return {
                "bidder_id": bidder_id,
                "action": request.action,
                "file_id": file_id,
                "revision": self.revision(library, name),
                "changed": changed,
                "updated_at": self.updated_at(bidder_id, None, meta),
                "warning": warning,
            }

    def validate_images(self, bidder_id, text):
        paths = set(re.findall(r"[A-Za-z0-9_-]+/images/[A-Za-z0-9_.-]+", text))
        for path in paths:
            file_id = path.split("/")[0]
            try:
                _, parsed = self.sources.parsed(self.owned_file(bidder_id, file_id))
                if not any(
                    i.path == path
                    for s in parsed.sections
                    for b in s.blocks
                    for i in b.images
                ):
                    raise ValueError("图片未登记")
            except (BusinessError, ValueError) as exc:
                raise BusinessError(
                    "invalid_image_reference",
                    "摘要引用了未登记或非本库的图片",
                    http_status=422,
                ) from exc

    def owned_file(self, bidder_id, file_id):
        self.bidders.require(bidder_id)
        file = self.files.require(file_id)
        if (
            file.bidder_id != bidder_id
            or file.project_id
            or file.task_id
            or file.data_role != "evidence_material"
        ):
            raise NotFoundError("file_not_found", "当前资料库没有此文件")
        return file

    def preview(self, bidder_id, file_id):
        return self.sources.preview(
            self.owned_file(bidder_id, file_id),
            f"/api/bidders/{bidder_id}/files/{file_id}/preview",
        )

    def image(self, bidder_id, file_id, image_name, version):
        return self.sources.image(
            self.owned_file(bidder_id, file_id), image_name, version
        )

    def export(self, bidder_id, format):
        with self.lock(bidder_id):
            library = self.editable_snapshot(bidder_id)
            name = management(bidder_id).get("display_name", bidder_id)
            revision = self.revision(library, name)
            if format == "json":
                return library.model_dump_json(indent=2).encode(), revision, name
            known = {m.file_id: m for m in library.materials}
            parts = [f"# {name}", "资料来源标记仅表示编辑来源，不代表真实性已核验。"]
            for source in library.source_files:
                material = known[source.file_id]
                label = (
                    "人工修订" if material.provenance == "human_modified" else "AI 整理"
                )
                parts.append(
                    f"## {source.file_name}\n\n文件：{source.file_id}\n\n路径：{source.source_path}\n\n来源：{label}\n\n### 简洁摘要\n\n{material.bid_summary}\n\n### 详细摘要\n\n{material.evidence_summary}\n\n### 直接关系"
                )
                parts.append(
                    "\n".join(
                        f"- {source.file_id} → {target}：{relation}"
                        for relation, target in material.relations
                    )
                    or "无"
                )
            return "\n\n".join(parts).encode(), revision, name
