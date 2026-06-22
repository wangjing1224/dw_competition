"""招标项目上传和同步规则初始化接口。"""

from fastapi import APIRouter, File, Form, UploadFile

from app.core.errors import BusinessError
from app.services.container import build_project_service

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.post("")
async def create_project(
    files: list[UploadFile] = File(...),
    relative_paths: list[str] | None = Form(default=None),
) -> dict[str, str]:
    if not files:
        raise BusinessError("empty_project_files", "招标文件不能为空")
    paths = relative_paths or [file.filename or "unnamed" for file in files]
    if len(paths) != len(files):
        raise BusinessError("invalid_relative_paths", "文件和相对路径数量不一致")
    project = await build_project_service().create_project(
        [
            (file.filename or "unnamed", path, file.file)
            for file, path in zip(files, paths, strict=True)
        ]
    )
    return {"project_id": project.project_id, "status": project.status}
