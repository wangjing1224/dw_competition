"""独立资料库管理入口；支持失败恢复，不开放追加、整库重跑或删除。"""

import asyncio
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, File, Form, Header, Query, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from app.api.bidder_models import BidderEdit
from app.api.source_preview import preview_response
from app.core.errors import BusinessError, NotFoundError
from app.services.background_runner import background_runner
from app.services.container import build_bidder_service
from app.services.evidence_library_service import EvidenceLibraryService
from app.storage.bidder_management import update_management
from app.storage.repositories import BidderRepository

router = APIRouter(prefix="/api/bidders", tags=["bidders"])


async def process_received_bidder(bidder_id):
    # 工厂也放后台，缺 Key 或初始化失败能在已受理库中看到终态。
    try:
        await build_bidder_service().process_bidder(bidder_id)
    except BaseException:
        if BidderRepository().require(bidder_id).status == "processing_evidence":
            BidderRepository().update_status(
                bidder_id,
                "evidence_failed",
                error_code="initialization_failed",
                error_message="后台入库初始化失败，请查看服务日志",
            )
            update_management(
                bidder_id,
                phase="failed",
                failed_phase="received",
                trace_status="unavailable",
            )
        raise


async def resume_received_bidder(bidder_id):
    try:
        await build_bidder_service().resume_bidder(bidder_id, claimed=True)
    except BaseException:
        if BidderRepository().require(bidder_id).status == "processing_evidence":
            BidderRepository().update_status(
                bidder_id,
                "evidence_failed",
                error_code="initialization_failed",
                error_message="后台恢复初始化失败，请查看服务日志；已保存资料保留",
            )
            update_management(
                bidder_id, phase="failed", failed_phase="ingesting",
                trace_status="partial",
            )
        raise


@router.post("/{bidder_id}/resume", status_code=202)
async def resume_bidder(bidder_id: str):
    result = await asyncio.to_thread(EvidenceLibraryService().claim_resume, bidder_id)
    try:
        background_runner.submit(resume_received_bidder, bidder_id)
    except Exception:
        BidderRepository().update_status(
            bidder_id,
            "evidence_failed",
            error_code="resume_start_failed",
            error_message="重试任务未能启动，请稍后重试；已保存资料保留",
        )
        update_management(
            bidder_id, phase="failed", failed_phase="ingesting",
            trace_status="partial",
        )
        raise
    return result


@router.post("")
async def create_bidder(
    files: Annotated[list[UploadFile], File()],
    relative_paths: Annotated[list[str], Form()],
    display_name: Annotated[str, Form(min_length=1, max_length=100)],
    client_request_id: Annotated[UUID, Form()],
):
    if not display_name.strip():
        raise BusinessError(
            "invalid_display_name", "资料库名称不能为空", http_status=422
        )
    if len(files) != len(relative_paths):
        raise BusinessError(
            "invalid_relative_paths", "文件和相对路径数量不一致", http_status=422
        )
    result, created = await asyncio.to_thread(
        EvidenceLibraryService().receive,
        [
            (file.filename or "unnamed", path, file.file)
            for file, path in zip(files, relative_paths, strict=True)
        ],
        display_name.strip(),
        str(client_request_id),
    )
    if created:
        background_runner.submit(process_received_bidder, result["bidder_id"])
    return JSONResponse(
        result,
        status_code=202 if created else 200,
        headers={"Location": f"/api/bidders/{result['bidder_id']}"},
    )


@router.get("")
def list_bidders(
    page: int = Query(1, ge=1),
    page_size: int = Query(12, ge=1, le=100),
    q: str | None = Query(None, max_length=100),
    status: Literal[
        "uploading_evidence", "processing_evidence", "evidence_ready", "evidence_failed"
    ]
    | None = None,
    attention_only: bool = False,
    client_request_id: UUID | None = None,
):
    return EvidenceLibraryService().list(
        page=page,
        page_size=page_size,
        q=q,
        status=status,
        attention_only=attention_only,
        client_request_id=str(client_request_id) if client_request_id else None,
    )


@router.get("/{bidder_id}")
def get_bidder(bidder_id: str, response: Response):
    result = EvidenceLibraryService().detail(bidder_id)
    response.headers["Cache-Control"] = "no-store"
    if result["revision"]:
        response.headers["ETag"] = f'"{result["revision"]}"'
    return result


@router.get("/{bidder_id}/progress")
def progress(
    bidder_id: str,
    response: Response,
    include_trace: bool = False,
    after_seq: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
):
    response.headers["Cache-Control"] = "no-store"
    return EvidenceLibraryService().progress(bidder_id, include_trace, after_seq, limit)


@router.get("/{bidder_id}/trace/{span_id}")
def trace_detail(bidder_id: str, span_id: str, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return EvidenceLibraryService().trace_detail(bidder_id, span_id)


@router.post("/{bidder_id}/edit")
def edit(
    bidder_id: str,
    body: BidderEdit,
    response: Response,
    if_match: str | None = Header(None),
):
    result = EvidenceLibraryService().edit(bidder_id, body, if_match)
    response.headers["ETag"] = f'"{result["revision"]}"'
    return result


@router.get("/{bidder_id}/files/{file_id}/preview")
def preview(
    bidder_id: str,
    file_id: str,
    image_name: str | None = None,
    version: str | None = None,
):
    return preview_response(
        EvidenceLibraryService(), bidder_id, file_id, image_name, version
    )


@router.get("/{bidder_id}/files/{file_id}/download")
def download(bidder_id: str, file_id: str):
    service = EvidenceLibraryService()
    file = service.owned_file(bidder_id, file_id)
    path = service.store.resolve(file_id)
    if not path.is_file():
        raise NotFoundError("source_file_unavailable", "已登记原件缺失")
    return FileResponse(path, filename=file.file_name)


@router.get("/{bidder_id}/export")
def export(bidder_id: str, format: Literal["json", "md"] = "json"):
    content, revision, name = EvidenceLibraryService().export(bidder_id, format)
    return Response(
        content,
        media_type="application/json"
        if format == "json"
        else "text/markdown; charset=utf-8",
        headers={
            "ETag": f'"{revision}"',
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name + '_资料库.' + format, safe='')}",
        },
    )
