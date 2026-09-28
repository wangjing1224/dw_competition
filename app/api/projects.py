"""规则库上传、后台进度、审阅维护和来源读取（仅 GET / POST）。"""

import asyncio
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, File, Form, Header, Query, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from app.api.project_models import ProjectEdit
from app.api.source_preview import preview_response
from app.core.errors import BusinessError
from app.services.background_runner import background_runner
from app.services.container import build_project_service
from app.services.rule_library_service import RuleLibraryService

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.post("")
async def create_project(
    files: Annotated[list[UploadFile], File()],
    client_request_id: Annotated[UUID, Form()],
    relative_paths: Annotated[list[str] | None, Form()] = None,
    display_name: Annotated[str | None, Form(min_length=1, max_length=120)] = None,
):
    if not files:
        raise BusinessError("empty_project_files", "招标文件不能为空")
    paths = relative_paths or [file.filename or "unnamed" for file in files]
    if len(paths) != len(files):
        raise BusinessError("invalid_relative_paths", "文件和相对路径数量不一致")
    if display_name is not None and not display_name.strip():
        raise BusinessError(
            "invalid_display_name", "规则库名称不能只有空白", http_status=422
        )
    result, created = await asyncio.to_thread(
        RuleLibraryService().receive,
        [
            (file.filename or "unnamed", path, file.file)
            for file, path in zip(files, paths, strict=True)
        ],
        display_name.strip() if display_name else None,
        str(client_request_id),
    )
    if created:
        background_runner.submit(
            build_project_service().process_project, result["project_id"]
        )
    return JSONResponse(
        result,
        status_code=202 if created else 200,
        headers={"Location": f"/api/projects/{result['project_id']}"},
    )


@router.get("")
def list_projects(
    page: int = Query(1, ge=1),
    page_size: int = Query(12, ge=1, le=100),
    q: str | None = Query(None, max_length=120),
    status: Literal["processing_rules", "rule_ready", "rule_failed"] | None = None,
    attention_only: bool = False,
    client_request_id: UUID | None = None,
):
    return RuleLibraryService().list(
        page=page,
        page_size=page_size,
        q=q,
        status=status,
        attention_only=attention_only,
        client_request_id=str(client_request_id) if client_request_id else None,
    )


@router.get("/{project_id}")
def project_detail(project_id: str, response: Response):
    result = RuleLibraryService().detail(project_id)
    if result["revision"]:
        response.headers["ETag"] = f'"{result["revision"]}"'
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/{project_id}/progress")
def project_progress(
    project_id: str,
    response: Response,
    include_trace: bool = False,
    after_seq: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
):
    response.headers["Cache-Control"] = "no-store"
    return RuleLibraryService().progress(project_id, include_trace, after_seq, limit)


@router.get("/{project_id}/trace/{span_id}")
def trace_detail(project_id: str, span_id: str, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return RuleLibraryService().trace_detail(project_id, span_id)


@router.post("/{project_id}/edit")
def edit_project(
    project_id: str,
    body: ProjectEdit,
    response: Response,
    if_match: str | None = Header(None),
):
    result = RuleLibraryService().edit(project_id, body, if_match)
    response.status_code = 201 if body.action == "add_rule" else 200
    response.headers["ETag"] = f'"{result["revision"]}"'
    return result


@router.get("/{project_id}/files/{file_id}/preview")
def file_preview(
    project_id: str,
    file_id: str,
    image_name: str | None = None,
    version: str | None = None,
):
    return preview_response(
        RuleLibraryService(), project_id, file_id, image_name, version
    )


@router.get("/{project_id}/files/{file_id}/download")
def download_file(project_id: str, file_id: str):
    service = RuleLibraryService()
    file = service.owned_file(project_id, file_id)
    path = service.store.resolve(file_id)
    if not path.is_file():
        raise BusinessError(
            "source_file_unavailable", "来源原件丢失或不可读取", http_status=500
        )
    return FileResponse(path, filename=file.file_name)


@router.get("/{project_id}/export")
def export(project_id: str, format: Literal["json", "md"] = Query(...)):
    content, revision = RuleLibraryService().export(project_id, format)
    return Response(
        content,
        media_type="application/json"
        if format == "json"
        else "text/markdown; charset=utf-8",
        headers={
            "ETag": f'"{revision}"',
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(project_id + '_规则库.' + format)}",
        },
    )
