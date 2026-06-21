# 任务 03：SQLite 与文件存储

## 1. 目标

实现项目、投标方、文件和审核任务的持久化，以及安全、可追踪的本地文件存储。该任务只负责存储，不执行解析和审核。

## 2. 建议文件

```text
app/storage/
├── database.py
├── schema.py
├── repositories.py
└── file_store.py
```

### 2.1 文件职责与伪代码归属

| 文件 | 职责 | 对应伪代码 |
|---|---|---|
| `app/storage/database.py` | SQLite 连接、事务提交/回滚和启动初始化 | 4.1、4.2 |
| `app/storage/schema.py` | 四张业务表、索引、外键和 `CREATE TABLE IF NOT EXISTS` DDL | 第 3 节、4.2 |
| `app/storage/repositories.py` | Project/Bidder/File/AuditTask Repository、业务状态更新、重启恢复 | 4.3、4.6、4.7 |
| `app/storage/file_store.py` | 文件流式落盘、路径规范化、ZIP 安全解压、原子移动 | 4.4、4.5 |
| `tests/unit/storage/test_database.py` | 连接、事务、外键和重复初始化测试 | 第 6 节数据库测试 |
| `tests/unit/storage/test_file_store.py` | 同名文件、路径穿越、ZIP Slip 和失败回滚测试 | 第 6 节文件测试 |
| `tests/integration/test_repositories.py` | Repository 归属隔离和业务状态更新测试 | 第 5、6 节 |

## 3. 数据表

按设计文档创建：

- `projects`
- `bidders`
- `files`
- `audit_tasks`

不创建 `schema_migrations` 等数据库版本表。当前 SQLite 只用于比赛演示期的本地元数据管理，数据库结构在应用启动时通过 `CREATE TABLE IF NOT EXISTS` 初始化。

可增加工程性字段：

- 乐观状态更新时间。
- 失败代码与失败阶段。
- 文件大小、扩展名、SHA-256。
- 原始相对路径和系统存储名。
- 证据文件的 `evidence_type`、分类置信度和分类状态。

不得把 DOCX、向量矩阵、JSON 和 Word 二进制内容写进 SQLite。

最小归属约束：

- `bidders` 使用 `UNIQUE(project_id, bidder_id)`。
- `files` 按数据角色使用组合外键：规则文件归属 project，资料文件归属 project + bidder，投标文件归属 project + bidder + task。
- `audit_tasks` 使用 `(project_id, bidder_id)` 组合外键指向 `bidders`，防止 task 绑定其他项目的 bidder。
- `audit_tasks` 增加 `UNIQUE(project_id, bidder_id, task_id)`，供投标文件组合外键引用。
- `files.data_role` 使用 `CHECK` 限制为约定枚举，并通过表级 `CHECK` 要求各角色必须填写对应 owner_id。

## 4. 核心伪代码

### 4.1 连接边界

```python
@contextmanager
def open_db():
    # 每次请求或后台任务独立创建连接，不跨线程共享。
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

### 4.2 数据库初始化

```python
def initialize_database() -> None:
    # TABLE_DDLS 只包含 projects、bidders、files、audit_tasks
    # 及其必要索引，不维护数据库版本和升级脚本。
    with open_db() as conn:
        for ddl in TABLE_DDLS:
            conn.execute(ddl)
        for ddl in INDEX_DDLS:
            conn.execute(ddl)
```

初始化必须可重复执行，应用每次启动均可调用。

### 4.3 创建业务对象

```python
def create_project() -> ProjectRecord:
    project_id = generate_id("project")
    with open_db() as conn:
        insert_project(
            conn,
            project_id=project_id,
            status="processing_rules",
        )
    return get_project(project_id)


def create_bidder(project_id: str) -> BidderRecord:
    require_project_exists(project_id)
    bidder_id = generate_id("bidder")
    # bidder 必须受 project 外键约束。
    insert_bidder(bidder_id, project_id, status="processing_evidence")
    return get_bidder(bidder_id)
```

### 4.4 文件安全落盘

```python
def save_uploaded_file(owner, upload, relative_path) -> StoredFile:
    safe_relative_path = normalize_relative_path(relative_path)

    # 拒绝绝对路径、父目录穿越、空路径和特殊设备名。
    if is_absolute_or_unsafe(safe_relative_path):
        raise BusinessError("unsafe_relative_path")

    file_id = generate_id("file")
    extension = validate_extension(upload.filename)
    storage_name = f"{file_id}{extension}"
    target = build_owner_directory(owner) / storage_name

    # 流式写入临时文件，限制大小，并计算哈希。
    temp_path, sha256, size = stream_to_temp(upload)
    try:
        atomic_move(temp_path, target)
        insert_file_record(
            file_id=file_id,
            source_path=safe_relative_path,
            storage_path=relative_to_data_root(target),
            sha256=sha256,
            size=size,
            parse_status="pending",
            **owner.ids,
        )
    except Exception:
        # 数据库写入失败时清理已经移动的目标文件。
        safe_unlink_if_exists(target)
        raise
    return StoredFile(...)
```

### 4.5 ZIP 安全解压

```python
def extract_zip_safely(zip_file, destination):
    for member in inspect_zip_members(zip_file):
        if member.is_symlink:
            raise BusinessError("zip_symlink_not_allowed")
        if member.uncompressed_size > per_file_limit:
            raise BusinessError("zip_entry_too_large")
        if total_uncompressed_size > total_limit:
            raise BusinessError("zip_too_large")

        target = safe_join(destination, member.relative_path)
        extract_member(member, target)
```

### 4.6 原子业务状态更新

```python
def transition_task(task_id, expected_status, new_status, **changes):
    with open_db() as conn:
        updated = conn.execute(
            """
            UPDATE audit_tasks
               SET status = ?, progress_json = ?, updated_at = ?
             WHERE task_id = ? AND status = ?
            """,
            (...),
        ).rowcount

        if updated != 1:
            raise StateConflict(task_id, expected_status)
```

这里的“状态更新”是审核任务的业务状态变化，例如 `running -> failed`，不是数据库 Schema 迁移。

### 4.7 中断任务恢复

```python
def mark_interrupted_tasks_failed():
    with open_db() as conn:
        tasks = select_tasks_by_status(conn, "running")
        for task in tasks:
            update_task(
                conn,
                task.task_id,
                status="failed",
                error_code="process_interrupted",
                error_message="应用重启导致进程内任务中断",
            )
```

## 5. Repository 约束

- 查询 bidder 时必须同时验证其 `project_id`。
- 查询 task 时返回其绑定的 `project_id` 和 `bidder_id`。
- 文件查询按 `data_role` 和完整 ID 范围执行。
- Repository 不返回数据库绝对路径给 API。
- 删除和覆盖不作为第一版公开能力。

## 6. 测试设计

- 应用首次启动能创建四张业务表和必要索引。
- 重复执行 `initialize_database` 不报错、不清空已有记录。
- 两个项目上传同名文件不会覆盖。
- `../`、绝对路径、ZIP Slip 和符号链接被拒绝。
- bidder 不能绑定不存在的 project。
- task 不能绑定其他项目下的 bidder。
- 两个线程不共享 SQLite 连接。
- 模拟进程重启后，遗留 `running` 任务变为 `failed/process_interrupted`。
- 文件写入失败时不留下“已保存”的数据库记录。

## 7. 验收标准

- 四张最小业务表和必要索引可自动初始化。
- 不包含 `schema_migrations` 表、`migrations.py` 或数据库版本升级机制。
- 文件通过 `file_id` 落盘，保留原始 `source_path`。
- 数据库仅存相对路径。
- 业务状态更新和重启恢复符合设计文档。
- 跨项目、投标方、任务的非法绑定被数据库约束和业务校验同时阻止。

## 8. 后续升级备注

当前 SQLite 数据仅服务本地演示，不承诺长期兼容和保留。开发阶段如表结构发生变化，可以删除 `data/app.db` 后由应用重新创建。

如果系统未来进入需要长期保存重要数据的生产阶段，应迁移到 PostgreSQL 等正式数据库，并在新数据库方案中重新设计迁移和备份机制；不在当前 SQLite 方案上继续叠加 Schema 迁移体系。
