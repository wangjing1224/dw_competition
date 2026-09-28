"""同类库名称唯一；不改写已有重名数据，不另存一套名称登记表。"""

import fcntl
import json
from contextlib import contextmanager
from typing import Literal

from app.core.errors import BusinessError
from app.storage.database import database_path

LibraryKind = Literal["project", "bidder"]
_SCOPES = {
    "project": ("projects", "project_management", "project_id", "规则库"),
    "bidder": ("bidders", "bidder_management", "bidder_id", "资料库"),
}


@contextmanager
def lock_library_names(kind: LibraryKind):
    # 创建涉及多次短事务。跨进程锁覆盖查重到名称写入，改名也共用此锁，
    # 避免两个并发请求都通过查重；不同类型的库互不阻塞。
    if kind not in _SCOPES:
        raise ValueError("未知库类型")
    db_path = database_path()
    path = db_path.with_name(f"{db_path.name}.{kind}-names.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def ensure_library_name_available(db, kind: LibraryKind, name, *, exclude_id=None):
    if name is None:
        return  # 规则库未传管理名称时，随后生成不重名的默认名称。
    table, metadata, key, label = _SCOPES[kind]
    # 表名只来自固定映射，查询仍覆盖尚未生成摘要/规则、处理中和失败的库。
    rows = db.execute(
        f"SELECT owner.{key} AS id, meta.payload FROM {table} owner "
        f"LEFT JOIN {metadata} meta ON owner.{key}=meta.{key}"
    )
    for row in rows:
        if row["id"] == exclude_id:
            continue
        payload = json.loads(row["payload"]) if row["payload"] else {}
        existing = payload.get("display_name", row["id"])
        if existing.strip().casefold() == name.strip().casefold():
            raise BusinessError(
                "duplicate_library_name",
                f"{label}名称“{name}”已存在，请更换名称",
                http_status=409,
            )


def default_library_name(db, kind: LibraryKind, owner_id):
    """没传名称时以 ID 命名；若有人先用此 ID 作库名，仅给新默认名加后缀。"""
    name, suffix = owner_id, 2
    while True:
        try:
            ensure_library_name_available(db, kind, name, exclude_id=owner_id)
            return name
        except BusinessError as exc:
            if exc.code != "duplicate_library_name":
                raise
            name = f"{owner_id}（{suffix}）"
            suffix += 1
