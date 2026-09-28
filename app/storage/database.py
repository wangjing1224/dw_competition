"""SQLite 连接、事务边界和重复安全初始化。"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

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
        # 早期“资料绑定项目”的结构不在迁移范围；先检查，不能静默删库。
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(bidders)")
        }
        if "project_id" in columns:
            raise RuntimeError(
                "检测到旧版项目绑定资料库。请保留旧数据作为参考，"
                "在 configs/storage.yaml 为新版配置独立 data_root 和 database_path 后启动；程序未修改旧表。"
            )
        # 旧审核父表要求整批共用一组库。仅重建这两张表的约束，原件/ID不变。
        task_columns = list(connection.execute("PRAGMA table_info(audit_tasks)"))
        if any(row["name"] == "project_id" and row["notnull"] for row in task_columns):
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            try:
                for table in ("audit_tasks", "files"):
                    ddl = next(
                        d
                        for d in TABLE_DDLS
                        if f"CREATE TABLE IF NOT EXISTS {table} (" in d
                    )
                    connection.execute(ddl.replace(f"{table} (", f"{table}_v28 (", 1))
                    connection.execute(f"INSERT INTO {table}_v28 SELECT * FROM {table}")
                    connection.execute(f"DROP TABLE {table}")
                    connection.execute(f"ALTER TABLE {table}_v28 RENAME TO {table}")
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise RuntimeError("审核表迁移外键检查失败，已回滚")
                connection.execute("PRAGMA user_version = 28")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.execute("PRAGMA foreign_keys = ON")
        for ddl in TABLE_DDLS:
            connection.execute(ddl)
        for ddl in INDEX_DDLS:
            connection.execute(ddl)
