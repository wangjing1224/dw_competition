"""审核任务创建、后台启动和状态查询接口。"""

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import FileResponse

from app.core.errors import BusinessError
from app.services.container import build_audit_task_service
from app.services.audit_task_service import query_audit_task
from app.storage.file_store import FileStore
from app.storage.repositories import AuditTaskRepository

router = APIRouter(prefix="/api", tags=["audit-tasks"])


@router.post("/projects/{project_id}/bidders/{bidder_id}/audit-tasks")
async def create_audit_task(
    project_id: str,
    bidder_id: str,
    files: list[UploadFile] = File(...),
    relative_paths: list[str] | None = Form(default=None),
) -> dict[str, str]:
    if not files:
        raise BusinessError("empty_audit_files", "审核测试集不能为空")
    paths = relative_paths or [file.filename or "unnamed" for file in files]
    if len(paths) != len(files):
        raise BusinessError("invalid_relative_paths", "文件和相对路径数量不一致")
    task = await build_audit_task_service().create_task(
        project_id,
        bidder_id,
        [
            (file.filename or "unnamed", path, file.file)
            for file, path in zip(files, paths, strict=True)
        ],
    )
    return {"task_id": task.task_id, "status": task.status}


@router.get("/audit-tasks/{task_id}")
async def get_audit_task(task_id: str) -> dict[str, object]:
    return query_audit_task(task_id)


@router.get("/audit-tasks/{task_id}/outputs/json")
async def download_json_output(task_id: str) -> FileResponse:
    """只允许下载当前任务记录中已登记的 JSON，不接收任意文件路径。"""

    task = AuditTaskRepository().require(task_id)
    if not task.json_path:
        raise BusinessError("json_output_not_ready", "JSON 结果尚未生成", http_status=404)
    path = FileStore().resolve_relative(task.json_path)
    return FileResponse(
        path,
        media_type="application/json",
        filename=path.name,
    )


@router.get("/audit-tasks/{task_id}/outputs/word/{output_index}")
async def download_word_output(task_id: str, output_index: int) -> FileResponse:
    """按任务中的 Word 输出序号下载，防止路径穿越和跨任务读取。"""

    task = AuditTaskRepository().require(task_id)
    paths = task.word_paths or []
    if output_index < 0 or output_index >= len(paths):
        raise BusinessError("word_output_not_found", "Word 报告不存在", http_status=404)
    path = FileStore().resolve_relative(paths[output_index])
    return FileResponse(
        path,
        media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        filename=path.name,
    )
