-- Xuheng extension: explicit constraints, immutable evidence, versioned feedback.
CREATE TABLE IF NOT EXISTS xh_rule_sets (
    novel_id TEXT PRIMARY KEY REFERENCES novels(id) ON DELETE CASCADE,
    revision TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS xh_rule_profiles (
    novel_id TEXT NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
    id TEXT NOT NULL, name TEXT NOT NULL,
    PRIMARY KEY (novel_id, id), UNIQUE (novel_id, name)
);
CREATE TABLE IF NOT EXISTS xh_rule_terms (
    novel_id TEXT NOT NULL, profile_id TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('aliases', 'allowed_magic', 'forbidden_actions')),
    value TEXT NOT NULL,
    PRIMARY KEY (novel_id, profile_id, category, value),
    FOREIGN KEY (novel_id, profile_id) REFERENCES xh_rule_profiles(novel_id, id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS xh_review_runs (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
    chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    chapter_number INTEGER NOT NULL,
    content_hash TEXT NOT NULL, content_snapshot TEXT NOT NULL,
    rules_hash TEXT NOT NULL, engine_version TEXT NOT NULL,
    created_at TEXT NOT NULL, elapsed_ms REAL NOT NULL,
    UNIQUE (novel_id, chapter_id, content_hash, rules_hash, engine_version)
);
CREATE INDEX IF NOT EXISTS idx_xh_run_chapter ON xh_review_runs(novel_id, chapter_id, created_at);
CREATE TABLE IF NOT EXISTS xh_review_issues (
    run_id TEXT NOT NULL REFERENCES xh_review_runs(id) ON DELETE CASCADE,
    id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('conflict', 'uncertain')),
    entity_id TEXT, entity_name TEXT, action TEXT NOT NULL,
    start INTEGER NOT NULL CHECK(start >= 0), end INTEGER NOT NULL CHECK(end > start),
    quote TEXT NOT NULL, source_id TEXT, source_quote TEXT NOT NULL, source_hash TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'confirmed', 'ignored')),
    feedback_version INTEGER NOT NULL DEFAULT 0,
    feedback_token TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, id)
);
CREATE TABLE IF NOT EXISTS xh_feedback_events (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, issue_id TEXT NOT NULL,
    status TEXT NOT NULL, version INTEGER NOT NULL, created_at TEXT NOT NULL,
    FOREIGN KEY (run_id, issue_id) REFERENCES xh_review_issues(run_id, id) ON DELETE CASCADE
);
