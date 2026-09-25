"""独立投标方资料库的整批创建与查询；没有完成后追加上传入口。"""

from dataclasses import asdict

from fastapi import APIRouter, File, Form, UploadFile

from app.core.config import get_settings
from app.core.errors import BusinessError
from app.evidence.storage import load_library, progress_path
from app.models.evidence import EvidenceIngestionProgress
from app.services.container import build_bidder_service
from app.storage.repositories import BidderRepository

router = APIRouter(prefix="/api/bidders", tags=["bidders"])


@router.post("")
async def create_bidder(
    files: list[UploadFile] = File(...),
    relative_paths: list[str] | None = Form(default=None),
) -> dict[str, str]:
    if not files:
        raise BusinessError("empty_evidence_files", "投标方资料不能为空")
    paths = relative_paths or [file.filename or "unnamed" for file in files]
    if len(paths) != len(files):
        raise BusinessError("invalid_relative_paths", "文件和相对路径数量不一致")
    bidder = await build_bidder_service().create_bidder(
        [
            (file.filename or "unnamed", path, file.file)
            for file, path in zip(files, paths, strict=True)
        ],
    )
    return {
        "bidder_id": bidder.bidder_id,
        "status": bidder.status,
    }


@router.get("/{bidder_id}")
def get_bidder(bidder_id: str) -> dict:
    bidder = BidderRepository().require(bidder_id)
    settings = get_settings()
    library = load_library(bidder_id, settings)
    path = progress_path(bidder_id, settings)
    progress = (
        EvidenceIngestionProgress.model_validate_json(path.read_text("utf-8"))
        if path.exists()
        else None
    )
    return {
        **asdict(bidder),
        "library": library.model_dump(mode="json") if library else None,
        "progress": progress.model_dump(mode="json") if progress else None,
    }
