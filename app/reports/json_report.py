"""AuditResult 严格校验、任务级 JSON 原子写入和重新读取。"""

import os
from pathlib import Path
from typing import Any

from app.models.results import AuditResult
from app.storage.file_store import FileStore


class AuditResultOutputError(RuntimeError):
    """最终业务结果无法通过 Schema 或安全写入时抛出。"""


def validate_audit_result(value: Any) -> AuditResult:
    return value if isinstance(value, AuditResult) else AuditResult.model_validate(value)


def write_json_result(
    task_id: str,
    value: Any,
    file_store: FileStore | None = None,
) -> str:
    """写入临时文件后重新校验，再原子替换正式 JSON。"""

    store = file_store or FileStore()
    result = validate_audit_result(value)
    directory = store.task_output_dir(task_id)
    path = directory / f"{task_id}_审核结果.json"
    temporary = path.with_suffix(".json.tmp")
    try:
        temporary.write_text(
            result.model_dump_json(indent=2, exclude_none=False),
            encoding="utf-8",
        )
        AuditResult.model_validate_json(temporary.read_text("utf-8"))
        os.replace(temporary, path)
        return store.relative_to_data_root(path)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        raise AuditResultOutputError(str(exc)) from exc


def read_json_result(
    relative_path: str,
    file_store: FileStore | None = None,
) -> AuditResult:
    store = file_store or FileStore()
    return AuditResult.model_validate_json(
        store.resolve_relative(relative_path).read_text("utf-8")
    )
