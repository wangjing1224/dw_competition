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
        bidder_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        error_code TEXT,
        error_message TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_tasks (
        task_id TEXT PRIMARY KEY,
        project_id TEXT,
        bidder_id TEXT,
        status TEXT NOT NULL,
        progress_json TEXT NOT NULL DEFAULT '{}',
        error_code TEXT,
        error_message TEXT,
        json_path TEXT,
        word_paths_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (project_id, bidder_id, task_id),
        FOREIGN KEY (project_id) REFERENCES projects(project_id),
        FOREIGN KEY (bidder_id) REFERENCES bidders(bidder_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS files (
        file_id TEXT PRIMARY KEY,
        project_id TEXT,
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
        FOREIGN KEY (bidder_id) REFERENCES bidders(bidder_id),
        FOREIGN KEY (task_id) REFERENCES audit_tasks(task_id),
        CHECK (
            (data_role = 'rule_source' AND project_id IS NOT NULL
                AND bidder_id IS NULL AND task_id IS NULL)
            OR
            (data_role = 'evidence_material'
                AND project_id IS NULL AND bidder_id IS NOT NULL AND task_id IS NULL)
            OR
            (data_role = 'bid_document'
                AND project_id IS NOT NULL AND bidder_id IS NOT NULL AND task_id IS NOT NULL)
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

TABLE_DDLS += (
    """CREATE TABLE IF NOT EXISTS project_management (
        project_id TEXT PRIMARY KEY REFERENCES projects(project_id), payload TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS project_uploads (
        request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
        project_id TEXT REFERENCES projects(project_id), state TEXT NOT NULL,
        error_json TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS project_trace (
        project_id TEXT NOT NULL REFERENCES projects(project_id), seq INTEGER NOT NULL,
        span_id TEXT NOT NULL, node TEXT NOT NULL, detail TEXT NOT NULL,
        PRIMARY KEY(project_id, seq)
    )""",
)

INDEX_DDLS = (
    "CREATE INDEX IF NOT EXISTS idx_bidder_trace_span ON bidder_trace(bidder_id,span_id,seq)",
    "CREATE INDEX IF NOT EXISTS idx_project_trace_span ON project_trace(project_id,span_id,seq)",
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

TABLE_DDLS += (
    """CREATE TABLE IF NOT EXISTS bidder_management (
        bidder_id TEXT PRIMARY KEY REFERENCES bidders(bidder_id), payload TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS bidder_uploads (
        request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
        bidder_id TEXT REFERENCES bidders(bidder_id), state TEXT NOT NULL, error_json TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS bidder_trace (
        bidder_id TEXT NOT NULL REFERENCES bidders(bidder_id), seq INTEGER NOT NULL,
        span_id TEXT NOT NULL, node TEXT NOT NULL, detail TEXT NOT NULL,
        PRIMARY KEY(bidder_id, seq)
    )""",
    """CREATE TABLE IF NOT EXISTS audit_management (
        task_id TEXT PRIMARY KEY REFERENCES audit_tasks(task_id), payload TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS audit_task_files (
        task_id TEXT NOT NULL REFERENCES audit_tasks(task_id),
        file_id TEXT NOT NULL REFERENCES files(file_id),
        upload_index INTEGER NOT NULL,
        project_id TEXT NOT NULL REFERENCES projects(project_id),
        bidder_id TEXT NOT NULL REFERENCES bidders(bidder_id),
        payload TEXT NOT NULL,
        PRIMARY KEY(task_id,file_id), UNIQUE(task_id,upload_index)
    )""",
    """CREATE TABLE IF NOT EXISTS audit_uploads (
        request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
        task_id TEXT REFERENCES audit_tasks(task_id), state TEXT NOT NULL, error_json TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS audit_trace (
        task_id TEXT NOT NULL REFERENCES audit_tasks(task_id), seq INTEGER NOT NULL,
        span_id TEXT NOT NULL, node TEXT NOT NULL, detail TEXT NOT NULL,
        PRIMARY KEY(task_id,seq)
    )""",
)

INDEX_DDLS += (
    "CREATE INDEX IF NOT EXISTS idx_audit_trace_span ON audit_trace(task_id,span_id,seq)",
    """CREATE TRIGGER IF NOT EXISTS audit_file_binding_insert
    BEFORE INSERT ON audit_task_files BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM files WHERE file_id=NEW.file_id AND task_id=NEW.task_id
        AND project_id=NEW.project_id AND bidder_id=NEW.bidder_id AND data_role='bid_document'
      ) THEN RAISE(ABORT, 'audit file binding mismatch') END;
    END""",
    """CREATE TRIGGER IF NOT EXISTS audit_file_binding_immutable
    BEFORE UPDATE OF task_id,file_id,project_id,bidder_id ON audit_task_files BEGIN
      SELECT RAISE(ABORT, 'audit binding is immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS audit_file_owner_immutable
    BEFORE UPDATE OF task_id,project_id,bidder_id,data_role ON files
    WHEN EXISTS (SELECT 1 FROM audit_task_files WHERE file_id=OLD.file_id) BEGIN
      SELECT RAISE(ABORT, 'audit file owner is immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS legacy_audit_owner_guard BEFORE INSERT ON files
    WHEN NEW.task_id IS NOT NULL AND EXISTS (
      SELECT 1 FROM audit_tasks WHERE task_id=NEW.task_id AND project_id IS NOT NULL
      AND (project_id!=NEW.project_id OR bidder_id!=NEW.bidder_id)
    ) BEGIN SELECT RAISE(ABORT, 'audit owner mismatch'); END""",
)
