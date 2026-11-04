CREATE TABLE IF NOT EXISTS xh_retrieval_settings (
    id TEXT PRIMARY KEY CHECK(id = 'default'),
    reranker_model TEXT NOT NULL,
    dimensions INTEGER NOT NULL DEFAULT 1024,
    revision TEXT NOT NULL
);
