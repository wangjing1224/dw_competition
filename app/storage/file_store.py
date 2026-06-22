"""上传文件安全落盘、路径解析和 ZIP 安全解压。"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from app.core.config import get_settings
from app.core.errors import BusinessError
from app.models.common import generate_id
from app.storage.repositories import FileRecord, FileRepository


ALLOWED_EXTENSIONS = {".doc", ".docx", ".xlsx", ".txt", ".md", ".zip"}


@dataclass(frozen=True)
class FileOwner:
    project_id: str
    bidder_id: str | None = None
    task_id: str | None = None


def normalize_relative_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or ":" in path.parts[0]
    ):
        raise BusinessError("unsafe_relative_path", "文件相对路径不安全")
    return path.as_posix()


def safe_join(root: Path, relative_path: str) -> Path:
    target = (root / normalize_relative_path(relative_path)).resolve()
    resolved_root = root.resolve()
    if target != resolved_root and resolved_root not in target.parents:
        raise BusinessError("unsafe_relative_path", "目标路径越过存储目录")
    return target


class FileStore:
    def __init__(self, repository: FileRepository | None = None) -> None:
        self.settings = get_settings()
        self.data_root = self.settings.storage.data_root
        self.repository = repository or FileRepository()

    def save(
        self,
        *,
        owner: FileOwner,
        data_role: str,
        stream: BinaryIO,
        file_name: str,
        relative_path: str,
    ) -> FileRecord:
        source_path = normalize_relative_path(relative_path)
        extension = Path(file_name).suffix.lower()
        if extension not in ALLOWED_EXTENSIONS:
            raise BusinessError(
                "unsupported_file_extension",
                f"暂不支持文件格式: {extension or '无扩展名'}",
            )

        file_id = generate_id("file")
        owner_path = Path(owner.project_id)
        if owner.bidder_id:
            owner_path /= owner.bidder_id
        if owner.task_id:
            owner_path /= owner.task_id
        target_dir = self.data_root / "uploads" / data_role / owner_path
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{file_id}{extension}"

        digest = hashlib.sha256()
        size = 0
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=target_dir, prefix=".upload-", delete=False
            ) as temporary:
                temp_path = Path(temporary.name)
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > self.settings.storage.max_upload_bytes:
                        raise BusinessError("file_too_large", "上传文件超过大小限制")
                    digest.update(chunk)
                    temporary.write(chunk)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temp_path, target)
            record = FileRecord(
                file_id=file_id,
                project_id=owner.project_id,
                bidder_id=owner.bidder_id,
                task_id=owner.task_id,
                data_role=data_role,
                file_name=Path(file_name).name,
                source_path=source_path,
                storage_path=target.relative_to(self.data_root).as_posix(),
                extension=extension,
                size_bytes=size,
                sha256=digest.hexdigest(),
                parse_status="pending",
            )
            return self.repository.create(record)
        except Exception:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise

    def resolve(self, file_id: str) -> Path:
        record = self.repository.require(file_id)
        return safe_join(self.data_root, record.storage_path)

    def relative_to_data_root(self, path: Path) -> str:
        """将对外持久化路径统一转换为 data_root 下的 POSIX 相对路径。"""

        return path.resolve().relative_to(self.data_root.resolve()).as_posix()

    def resolve_relative(self, relative_path: str) -> Path:
        """解析数据库中的相对路径，并再次执行目录越界检查。"""

        return safe_join(self.data_root, relative_path)

    def task_output_dir(self, task_id: str) -> Path:
        """返回任务独立输出目录，避免不同审核任务覆盖结果。"""

        if not task_id or "/" in task_id or "\\" in task_id or ".." in task_id:
            raise BusinessError("unsafe_task_id", "任务 ID 不安全")
        path = self.data_root / "outputs" / task_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def extract_zip_safely(self, archive: Path, destination: Path) -> list[Path]:
        extracted: list[Path] = []
        total_size = 0
        destination.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as zip_file:
            for member in zip_file.infolist():
                unix_mode = member.external_attr >> 16
                if unix_mode and (unix_mode & 0o170000) == 0o120000:
                    raise BusinessError(
                        "zip_symlink_not_allowed", "ZIP 中不允许符号链接"
                    )
                if member.file_size > self.settings.storage.max_zip_entry_bytes:
                    raise BusinessError("zip_entry_too_large", "ZIP 单个文件过大")
                total_size += member.file_size
                if total_size > self.settings.storage.max_zip_total_bytes:
                    raise BusinessError("zip_too_large", "ZIP 解压后总大小过大")
                if member.is_dir():
                    continue

                target = safe_join(destination, member.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zip_file.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                extracted.append(target)
        return extracted
