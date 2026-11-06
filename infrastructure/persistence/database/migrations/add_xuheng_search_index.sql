-- Older chapter tables may omit the key already required by chapter_reviews.
CREATE UNIQUE INDEX IF NOT EXISTS idx_xh_chapters_work_number ON chapters(novel_id, number);

CREATE TABLE IF NOT EXISTS xh_search_jobs (
    novel_id TEXT PRIMARY KEY REFERENCES novels(id) ON DELETE CASCADE,
    id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('queued','completed','cancelled','failed')),
    error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_xh_search_queue ON xh_search_jobs(status, updated_at);

CREATE TABLE IF NOT EXISTS xh_search_documents (
    -- Chapter triggers own this relationship so legacy chapter schemas can still write.
    chapter_id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    index_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_xh_search_scope ON xh_search_documents(novel_id, number);

CREATE TABLE IF NOT EXISTS xh_search_chunks (
    id TEXT PRIMARY KEY,
    chapter_id TEXT NOT NULL REFERENCES xh_search_documents(chapter_id) ON DELETE CASCADE,
    start INTEGER NOT NULL,
    end INTEGER NOT NULL,
    text TEXT NOT NULL,
    term_count INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_xh_search_chunks_chapter ON xh_search_chunks(chapter_id);

CREATE TABLE IF NOT EXISTS xh_search_postings (
    chunk_id TEXT NOT NULL REFERENCES xh_search_chunks(id) ON DELETE CASCADE,
    novel_id TEXT NOT NULL,
    term TEXT NOT NULL,
    frequency INTEGER NOT NULL,
    PRIMARY KEY(chunk_id, term)
);
CREATE INDEX IF NOT EXISTS idx_xh_search_postings_scope ON xh_search_postings(novel_id, term);

CREATE TRIGGER IF NOT EXISTS xh_search_chapter_insert AFTER INSERT ON chapters BEGIN
    UPDATE xh_search_jobs SET status='queued', updated_at=CURRENT_TIMESTAMP
    WHERE novel_id=NEW.novel_id AND status='completed';
END;

CREATE TRIGGER IF NOT EXISTS xh_search_chapter_change AFTER UPDATE OF content, number, novel_id ON chapters
WHEN OLD.content IS NOT NEW.content OR OLD.number IS NOT NEW.number OR OLD.novel_id IS NOT NEW.novel_id
BEGIN
    DELETE FROM xh_search_documents WHERE chapter_id=OLD.id;
    UPDATE xh_search_jobs SET status='queued', updated_at=CURRENT_TIMESTAMP
    WHERE novel_id IN (OLD.novel_id, NEW.novel_id) AND status='completed';
END;

CREATE TRIGGER IF NOT EXISTS xh_search_chapter_delete AFTER DELETE ON chapters BEGIN
    DELETE FROM xh_search_documents WHERE chapter_id=OLD.id;
END;
