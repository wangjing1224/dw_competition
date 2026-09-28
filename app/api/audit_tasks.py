"""逐文件绑定的批量审核管理接口；GET 不触发模型、解析或导出。"""

import asyncio
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, File, Form, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import TypeAdapter, ValidationError

from app.core.errors import BusinessError
from app.services.audit_batch_runner import fail_file, finish_batch, run_audit_batch
from app.services.audit_management_service import Assignment, AuditManagementService
from app.services.background_runner import background_runner
from app.storage.audit_management import file_rows

router = APIRouter(prefix="/api/audit-tasks", tags=["audit-tasks"])
HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


@router.post("")
async def create_audit_task(
    display_name: Annotated[str, Form(min_length=1, max_length=100)],
    client_request_id: Annotated[UUID, Form()],
    assignments: Annotated[str, Form()],
    files: Annotated[list[UploadFile], File()],
    relative_paths: Annotated[list[str], Form()],
):
    try:
        bindings = TypeAdapter(list[Assignment]).validate_json(assignments)
    except ValidationError as exc:
        raise BusinessError(
            "invalid_assignments", "文件与审核库绑定格式错误", http_status=422
        ) from exc
    if len(files) != len(relative_paths):
        raise BusinessError(
            "invalid_relative_paths", "文件与路径数量不一致", http_status=422
        )
    service = AuditManagementService()
    result, created = await asyncio.to_thread(
        service.receive,
        [
            (f.filename or "unnamed", p, f.file)
            for f, p in zip(files, relative_paths, strict=True)
        ],
        display_name,
        str(client_request_id),
        bindings,
    )
    if created:
        try:
            background_runner.submit(run_audit_batch, result["task_id"])
        except Exception as exc:
            for row in file_rows(result["task_id"]):
                fail_file(result["task_id"], row["file_id"], exc)
            finish_batch(service, result["task_id"])
            raise BusinessError(
                "queue_unavailable", "文件已接收，但后台任务未能启动", http_status=500
            ) from exc
    return JSONResponse(result, status_code=202 if created else 200, headers=HEADERS)


@router.get("")
def list_audit_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(12, ge=1, le=100),
    q: str | None = Query(None, max_length=100),
    group: Literal["all", "running", "completed", "attention"] = "all",
    client_request_id: UUID | None = None,
):
    return JSONResponse(
        AuditManagementService().list(
            page,
            page_size,
            q,
            group,
            str(client_request_id) if client_request_id else None,
        ),
        headers=HEADERS,
    )


@router.get("/{task_id}")
def get_audit_task(task_id: str):
    return JSONResponse(AuditManagementService().detail(task_id), headers=HEADERS)


@router.get("/{task_id}/files/{file_id}")
def get_file_report(task_id: str, file_id: str):
    return JSONResponse(
        AuditManagementService().report(task_id, file_id), headers=HEADERS
    )


@router.get("/{task_id}/progress")
def get_progress(
    task_id: str,
    include_trace: bool = False,
    file_id: str | None = None,
    after_seq: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
):
    return JSONResponse(
        AuditManagementService().progress(
            task_id, include_trace, file_id, after_seq, limit
        ),
        headers=HEADERS,
    )


@router.get("/{task_id}/trace/{span_id}")
def get_trace(task_id: str, span_id: str):
    return JSONResponse(
        AuditManagementService().trace_detail(task_id, span_id), headers=HEADERS
    )


@router.get("/{task_id}/files/{file_id}/sources/{source_file_id}/preview")
def preview_source(
    task_id: str,
    file_id: str,
    source_file_id: str,
    image_name: str | None = Query(None, min_length=1),
    version: str | None = None,
    source_key: str | None = None,
):
    result = AuditManagementService().preview(
        task_id, file_id, source_file_id, image_name, version, source_key
    )
    if isinstance(result, tuple):
        data, media_type = result
        return Response(data, media_type=media_type, headers=HEADERS)
    return JSONResponse(result, headers=HEADERS)


@router.get("/{task_id}/files/{file_id}/sources/{source_file_id}/download")
def download_source(task_id: str, file_id: str, source_file_id: str):
    service = AuditManagementService()
    record = service.source_record(task_id, file_id, source_file_id)
    path = service.store.resolve_relative(record.storage_path)
    if not path.is_file():
        raise BusinessError("source_unavailable", "已登记原件不可读取", http_status=500)
    return FileResponse(path, filename=record.file_name, headers=HEADERS)


@router.get("/{task_id}/outputs")
def download_output(
    task_id: str, format: Literal["json", "md", "word"], file_id: str | None = None
):
    path = AuditManagementService().output(task_id, format, file_id)
    return FileResponse(path, filename=path.name, headers=HEADERS)
