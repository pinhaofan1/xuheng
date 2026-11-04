CREATE TABLE IF NOT EXISTS xh_review_retrieval_usage (
    run_id TEXT PRIMARY KEY REFERENCES xh_review_runs(id) ON DELETE CASCADE,
    usage_json TEXT NOT NULL
);
