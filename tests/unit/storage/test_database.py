"""SQLite 初始化、外键和重复执行测试。"""

import sqlite3

from app.storage.database import initialize_database, open_db


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
