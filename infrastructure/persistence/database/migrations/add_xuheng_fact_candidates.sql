CREATE TABLE IF NOT EXISTS xh_fact_candidates (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
    source_chapter_id TEXT NOT NULL,
    source_number INTEGER NOT NULL,
    source_hash TEXT NOT NULL,
    source_start INTEGER NOT NULL,
    source_end INTEGER NOT NULL,
    source_quote TEXT NOT NULL,
    subject_name TEXT NOT NULL,
    assertion TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accept','coexist','reject')),
    version INTEGER NOT NULL DEFAULT 0,
    profile_id TEXT,
    replaces TEXT,
    applies_from INTEGER,
    applies_through INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_xh_fact_scope ON xh_fact_candidates(novel_id,status,source_number,id);

CREATE TABLE IF NOT EXISTS xh_fact_decisions (
    novel_id TEXT NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    fact_id TEXT NOT NULL REFERENCES xh_fact_candidates(id) ON DELETE CASCADE,
    request_json TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    rules_revision TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(novel_id,request_id)
);
CREATE INDEX IF NOT EXISTS idx_xh_fact_history ON xh_fact_decisions(novel_id,fact_id,created_at);
