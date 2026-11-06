CREATE TABLE IF NOT EXISTS xh_search_vector_jobs (
    novel_id TEXT PRIMARY KEY REFERENCES novels(id) ON DELETE CASCADE,
    id TEXT NOT NULL UNIQUE,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','completed','cancelled','failed')),
    claim TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_xh_search_vector_queue ON xh_search_vector_jobs(status, updated_at);

CREATE TABLE IF NOT EXISTS xh_search_vectors (
    novel_id TEXT NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    text TEXT NOT NULL,
    dimensions INTEGER NOT NULL CHECK(dimensions > 0),
    vector_json TEXT NOT NULL,
    PRIMARY KEY (novel_id, fingerprint, text)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS xh_search_vectors_document_published AFTER INSERT ON xh_search_documents
BEGIN
    UPDATE xh_search_vector_jobs SET status='queued', updated_at=CURRENT_TIMESTAMP
    WHERE novel_id=NEW.novel_id AND status='completed';
END;

CREATE TRIGGER IF NOT EXISTS xh_search_vectors_prune_deleted_chapter AFTER DELETE ON chapters
BEGIN
    UPDATE xh_search_jobs SET status='queued', updated_at=CURRENT_TIMESTAMP
    WHERE novel_id=OLD.novel_id AND status='completed';
END;
