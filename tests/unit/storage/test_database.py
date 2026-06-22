"""SQLite 初始化、外键和重复执行测试。"""

import sqlite3

from app.storage.database import initialize_database, open_db
from app.storage.ids import next_id


def test_initialize_database_is_idempotent(tmp_path):
    path = tmp_path / "app.db"
    initialize_database(path)
    initialize_database(path)

    with open_db(path) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert {
        "projects",
        "bidders",
        "files",
        "audit_tasks",
        "audit_events",
    } <= tables
    assert "schema_migrations" not in tables


def test_short_ids_increment_by_prefix(monkeypatch, tmp_path):
    path = tmp_path / "app.db"
    monkeypatch.setattr("app.storage.database.database_path", lambda: path)
    initialize_database(path)

    assert next_id("project") == "project-1"
    assert next_id("project") == "project-2"
    assert next_id("audit") == "audit-1"

    # 重新打开数据库后继续递增，而不是回到 1。
    assert next_id("project") == "project-3"
