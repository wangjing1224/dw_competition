"""投标方资料上传、分类和索引构建接口。"""

from fastapi import APIRouter, File, Form, UploadFile

from app.core.errors import BusinessError
from app.services.container import build_bidder_service

router = APIRouter(prefix="/api/projects", tags=["bidders"])


@router.post("/{project_id}/bidders")
async def create_bidder(
    project_id: str,
    files: list[UploadFile] = File(...),
    relative_paths: list[str] | None = Form(default=None),
) -> dict[str, str]:
    if not files:
        raise BusinessError("empty_evidence_files", "投标方资料不能为空")
    paths = relative_paths or [file.filename or "unnamed" for file in files]
    if len(paths) != len(files):
        raise BusinessError("invalid_relative_paths", "文件和相对路径数量不一致")
    bidder = await build_bidder_service().create_bidder(
        project_id,
        [
            (file.filename or "unnamed", path, file.file)
            for file, path in zip(files, paths, strict=True)
        ],
    )
    return {
        "project_id": bidder.project_id,
        "bidder_id": bidder.bidder_id,
        "status": bidder.status,
    }
