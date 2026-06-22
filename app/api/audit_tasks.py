"""审核任务创建、后台启动和状态查询接口。"""

from fastapi import APIRouter, File, Form, UploadFile

from app.core.errors import BusinessError
from app.services.container import build_audit_task_service
from app.services.audit_task_service import query_audit_task

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
