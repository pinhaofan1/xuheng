CREATE TABLE IF NOT EXISTS xh_aftermath_tasks (
    id TEXT PRIMARY KEY, novel_id TEXT NOT NULL, number INTEGER NOT NULL, content_hash TEXT NOT NULL,
    status TEXT NOT NULL, phase TEXT NOT NULL DEFAULT 'core', payload TEXT NOT NULL,
    result TEXT, owner TEXT, heartbeat REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(novel_id,number,content_hash)
);
CREATE INDEX IF NOT EXISTS xh_aftermath_pending ON xh_aftermath_tasks(status,heartbeat);
CREATE TABLE IF NOT EXISTS xh_aftermath_steps (
    task_id TEXT NOT NULL, step TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(task_id,step)
);
CREATE TABLE IF NOT EXISTS xh_aftermath_requests (
    task_id TEXT NOT NULL, step TEXT NOT NULL, slot INTEGER NOT NULL, attempt INTEGER NOT NULL,
    status TEXT NOT NULL, result TEXT, call_ids TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(task_id,step,slot,attempt)
);
CREATE TABLE IF NOT EXISTS xh_aftermath_resumes (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, confirmed_unknown INTEGER NOT NULL,
    note TEXT NOT NULL, created_at TEXT NOT NULL
);
