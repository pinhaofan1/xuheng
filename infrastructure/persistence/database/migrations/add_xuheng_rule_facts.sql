CREATE TABLE IF NOT EXISTS xh_rule_facts (
    novel_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (novel_id,profile_id,position),
    FOREIGN KEY (novel_id,profile_id) REFERENCES xh_rule_profiles(novel_id,id) ON DELETE CASCADE
);
