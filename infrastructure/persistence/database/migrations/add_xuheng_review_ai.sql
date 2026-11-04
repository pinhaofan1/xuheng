-- Xuheng full review: apply after add_xuheng_evidence_reviews.sql.
ALTER TABLE xh_review_runs ADD COLUMN mode TEXT NOT NULL DEFAULT 'rules';
ALTER TABLE xh_review_runs ADD COLUMN context_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE xh_review_runs ADD COLUMN model_name TEXT NOT NULL DEFAULT '';
ALTER TABLE xh_review_runs ADD COLUMN input_tokens INTEGER;
ALTER TABLE xh_review_runs ADD COLUMN output_tokens INTEGER;
ALTER TABLE xh_review_runs ADD COLUMN model_calls INTEGER NOT NULL DEFAULT 0;
ALTER TABLE xh_review_runs ADD COLUMN summary TEXT NOT NULL DEFAULT '';
ALTER TABLE xh_review_runs ADD COLUMN retrieval_mode TEXT NOT NULL DEFAULT '';
ALTER TABLE xh_review_issues ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'setting';
ALTER TABLE xh_review_issues ADD COLUMN source_chapter_id TEXT;
ALTER TABLE xh_review_issues ADD COLUMN source_chapter_number INTEGER;
ALTER TABLE xh_review_issues ADD COLUMN source_start INTEGER;
ALTER TABLE xh_review_issues ADD COLUMN source_end INTEGER;
ALTER TABLE xh_review_issues ADD COLUMN source_content_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE xh_review_issues ADD COLUMN origin TEXT NOT NULL DEFAULT 'rules';
CREATE TABLE IF NOT EXISTS xh_review_warnings (
    run_id TEXT NOT NULL REFERENCES xh_review_runs(id) ON DELETE CASCADE,
    message TEXT NOT NULL,
    PRIMARY KEY(run_id, message)
);
CREATE TABLE IF NOT EXISTS xh_review_sources (
    run_id TEXT NOT NULL REFERENCES xh_review_runs(id) ON DELETE CASCADE,
    id TEXT NOT NULL, chapter_id TEXT NOT NULL, chapter_number INTEGER NOT NULL,
    start INTEGER NOT NULL, end INTEGER NOT NULL, text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    PRIMARY KEY(run_id, id)
);
CREATE TABLE IF NOT EXISTS xh_history_index (
    chapter_id TEXT PRIMARY KEY REFERENCES chapters(id) ON DELETE CASCADE,
    novel_id TEXT NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL, index_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS xh_history_chunks (
    id TEXT PRIMARY KEY,
    chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    novel_id TEXT NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
    chapter_number INTEGER NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
    text TEXT NOT NULL, content_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_xh_history_work ON xh_history_chunks(novel_id, chapter_number);
CREATE TABLE IF NOT EXISTS xh_embedding_cache (
    cache_key TEXT PRIMARY KEY,
    vector_json TEXT NOT NULL
);
