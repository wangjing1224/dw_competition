"""使用 SQLite 序列表生成短小、持久且并发安全的业务 ID。"""

from app.storage.database import open_db


def next_id(prefix: str) -> str:
    """返回 `prefix-数字`，应用重启后继续递增而不是重新从 1 开始。"""

    if not prefix or not prefix.replace("-", "").isalnum():
        raise ValueError("ID 前缀只能包含字母、数字和连字符")

    with open_db() as connection:
        # 即使某个纯函数测试直接调用 ID 分配器，也能完成最小初始化；
        # 正常服务启动时仍由 initialize_database 统一建表。
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS id_sequences (
                prefix TEXT PRIMARY KEY,
                current_value INTEGER NOT NULL CHECK (current_value >= 0)
            )
            """
        )
        # SQLite 写事务会串行化更新；先确保该类型的序列存在，再原子加一。
        connection.execute(
            """
            INSERT OR IGNORE INTO id_sequences(prefix, current_value)
            VALUES (?, 0)
            """,
            (prefix,),
        )
        connection.execute(
            """
            UPDATE id_sequences
               SET current_value = current_value + 1
             WHERE prefix = ?
            """,
            (prefix,),
        )
        row = connection.execute(
            "SELECT current_value FROM id_sequences WHERE prefix = ?",
            (prefix,),
        ).fetchone()
    if row is None:
        raise RuntimeError(f"无法分配 ID: {prefix}")
    return f"{prefix}-{row['current_value']}"
