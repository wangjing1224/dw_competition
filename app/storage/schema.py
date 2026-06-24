"""业务表、观测事件表和短 ID 序列表 DDL。"""

TABLE_DDLS = (
    """
    CREATE TABLE IF NOT EXISTS id_sequences (
        prefix TEXT PRIMARY KEY,
        current_value INTEGER NOT NULL CHECK (current_value >= 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS projects (
        project_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        rule_set_path TEXT,
        error_code TEXT,
        error_message TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bidders (
        project_id TEXT NOT NULL,
        bidder_id TEXT NOT NULL,
        status TEXT NOT NULL,
        error_code TEXT,
        error_message TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (project_id, bidder_id),
        UNIQUE (bidder_id),
        FOREIGN KEY (project_id) REFERENCES projects(project_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_tasks (
        task_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        bidder_id TEXT NOT NULL,
        status TEXT NOT NULL,
        progress_json TEXT NOT NULL DEFAULT '{}',
        error_code TEXT,
        error_message TEXT,
        json_path TEXT,
        word_paths_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (project_id, bidder_id, task_id),
        FOREIGN KEY (project_id, bidder_id)
            REFERENCES bidders(project_id, bidder_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS files (
        file_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        bidder_id TEXT,
        task_id TEXT,
        data_role TEXT NOT NULL CHECK (
            data_role IN ('rule_source', 'evidence_material', 'bid_document')
        ),
        file_name TEXT NOT NULL,
        source_path TEXT NOT NULL,
        storage_path TEXT NOT NULL UNIQUE,
        extension TEXT NOT NULL,
        size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
        sha256 TEXT NOT NULL,
        parse_status TEXT NOT NULL,
        evidence_type TEXT,
        classification_status TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (project_id) REFERENCES projects(project_id),
        FOREIGN KEY (project_id, bidder_id)
            REFERENCES bidders(project_id, bidder_id),
        FOREIGN KEY (project_id, bidder_id, task_id)
            REFERENCES audit_tasks(project_id, bidder_id, task_id),
        CHECK (
            (data_role = 'rule_source' AND bidder_id IS NULL AND task_id IS NULL)
            OR
            (data_role = 'evidence_material'
                AND bidder_id IS NOT NULL AND task_id IS NULL)
            OR
            (data_role = 'bid_document'
                AND bidder_id IS NOT NULL AND task_id IS NOT NULL)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        actor TEXT NOT NULL,
        event_type TEXT NOT NULL,
        title TEXT NOT NULL,
        detail_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        FOREIGN KEY (task_id) REFERENCES audit_tasks(task_id)
    )
    """,
)

INDEX_DDLS = (
    "CREATE INDEX IF NOT EXISTS idx_bidders_project ON bidders(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_owner ON audit_tasks(project_id, bidder_id)",
    """
    CREATE INDEX IF NOT EXISTS idx_files_scope
    ON files(project_id, bidder_id, task_id, data_role)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_audit_events_task
    ON audit_events(task_id, event_id)
    """,
)
