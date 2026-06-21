# 任务 03：SQLite 与文件存储

## 1. 目标

实现项目、投标方、文件和审核任务的持久化，以及安全、可追踪的本地文件存储。该任务只负责存储，不执行解析和审核。

## 2. 建议文件

```text
app/storage/
├── database.py
├── schema.py
├── repositories.py
├── file_store.py
└── migrations.py
```

## 3. 数据表

按设计文档创建：

- `projects`
- `bidders`
- `files`
- `audit_tasks`

可增加工程性字段：

- 乐观状态更新时间。
- 失败代码与失败阶段。
- 文件大小、扩展名、SHA-256。
- 原始相对路径和系统存储名。

不得把 DOCX、向量矩阵、JSON 和 Word 二进制内容写进 SQLite。

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

### 4.2 创建业务对象

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

### 4.3 文件安全落盘

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
    return StoredFile(...)
```

### 4.4 ZIP 安全解压

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

### 4.5 原子状态更新

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

### 4.6 中断任务恢复

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

- 两个项目上传同名文件不会覆盖。
- `../`、绝对路径、ZIP Slip 和符号链接被拒绝。
- bidder 不能绑定不存在的 project。
- task 不能绑定其他项目下的 bidder。
- 两个线程不共享 SQLite 连接。
- 模拟进程重启后，遗留 `running` 任务变为 `failed/process_interrupted`。
- 文件写入失败时不留下“已保存”的数据库记录。

## 7. 验收标准

- 四张最小业务表和必要索引可自动初始化。
- 文件通过 `file_id` 落盘，保留原始 `source_path`。
- 数据库仅存相对路径。
- 状态迁移和重启恢复符合设计文档。
- 跨项目、投标方、任务的非法绑定被数据库约束和业务校验同时阻止。
