"""SQLite 连接、事务边界和重复安全初始化。"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.core.config import get_settings
from app.storage.schema import INDEX_DDLS, TABLE_DDLS


def database_path() -> Path:
    return get_settings().storage.database_path


@contextmanager
def open_db(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    target = path or database_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database(path: Path | None = None) -> None:
    with open_db(path) as connection:
        # 本轮不迁移旧业务数据，更不能静默删库。先检查，避免半初始化。
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(bidders)")
        }
        if "project_id" in columns:
            raise RuntimeError(
                "检测到旧版项目绑定资料库。请保留旧数据作为参考，"
                "在 configs/storage.yaml 为新版配置独立 data_root 和 database_path 后启动；程序未修改旧表。"
            )
        for ddl in TABLE_DDLS:
            connection.execute(ddl)
        for ddl in INDEX_DDLS:
            connection.execute(ddl)
