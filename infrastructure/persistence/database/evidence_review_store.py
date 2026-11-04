"""Xuheng SQLite adapter. Writes use the existing single-writer transaction path.

An API success is returned only after its own row/revision is visible, rather
than interpreting an empty queue or a successful enqueue as a committed write.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from time import monotonic, sleep
from uuid import uuid4

from application.audit.evidence_models import ENGINE_VERSION, CharacterRule, RuleSet
from application.audit.services.evidence_review_service import ReviewConflict, ReviewNotFound, ReviewUnavailable


class EvidenceReviewStore:
    def __init__(self, db, write_timeout: float = 4.0):
        self.db = db
        self.write_timeout = write_timeout

    def require_novel(self, novel_id: str) -> None:
        if not self.db.fetch_one("SELECT id FROM novels WHERE id = ?", (novel_id,)):
            raise ReviewNotFound("未找到作品")

    def get_chapter(self, novel_id: str, number: int | None = None, chapter_id: str | None = None) -> dict:
        column, value = ("id", chapter_id) if chapter_id is not None else ("number", number)
        row = self.db.fetch_one(
            f"SELECT id, novel_id, number, content FROM chapters WHERE novel_id = ? AND {column} = ?",
            (novel_id, value),
        )
        if row is None:
            raise ReviewNotFound("未找到该作品中的章节")
        return row

    def get_rules(self, novel_id: str) -> RuleSet:
        rows = self.db.fetch_all(
            """SELECT s.revision, p.id, p.name, t.category, t.value
               FROM xh_rule_sets s LEFT JOIN xh_rule_profiles p ON p.novel_id = s.novel_id
               LEFT JOIN xh_rule_terms t ON t.novel_id = p.novel_id AND t.profile_id = p.id
               WHERE s.novel_id = ? ORDER BY p.id, t.category, t.value""", (novel_id,),
        )
        if not rows:
            return RuleSet()
        profiles: dict[str, dict] = {}
        for row in rows:
            if row["id"] is None:
                continue
            profile = profiles.setdefault(row["id"], {
                "id": row["id"], "name": row["name"], "aliases": [],
                "allowed_magic": [], "forbidden_actions": [],
                'facts': [],
            })
            if row["category"]:
                profile[row["category"]].append(row["value"])
        for row in self.db.fetch_all('SELECT profile_id,text FROM xh_rule_facts WHERE novel_id=? ORDER BY profile_id,position', (novel_id,)):
            if row['profile_id'] in profiles:
                profiles[row['profile_id']]['facts'].append(row['text'])
        return RuleSet(revision=rows[0]["revision"], profiles=[CharacterRule(**x) for x in profiles.values()])

    def save_rules(self, novel_id: str, rules: RuleSet) -> RuleSet:
        current = self.get_rules(novel_id)
        if current.revision != rules.revision:
            raise ReviewConflict("设定已在别处更新，请刷新后再保存")
        revision = uuid4().hex
        guard = "EXISTS (SELECT 1 FROM xh_rule_sets WHERE novel_id = ? AND revision = ?)"
        with self.db.transaction() as tx:
            tx.execute("INSERT OR IGNORE INTO xh_rule_sets(novel_id, revision) VALUES (?, '')", (novel_id,))
            tx.execute("UPDATE xh_rule_sets SET revision = ?, content_hash = ? WHERE novel_id = ? AND revision = ?", (revision, rules.content_hash(), novel_id, rules.revision))
            tx.execute(f"DELETE FROM xh_rule_profiles WHERE novel_id = ? AND {guard}", (novel_id, novel_id, revision))
            for p in rules.profiles:
                tx.execute(f"INSERT INTO xh_rule_profiles(novel_id, id, name) SELECT ?, ?, ? WHERE {guard}",
                           (novel_id, p.id, p.name, novel_id, revision))
                for category in ("aliases", "allowed_magic", "forbidden_actions"):
                    for value in getattr(p, category):
                        tx.execute(f"INSERT INTO xh_rule_terms(novel_id, profile_id, category, value) SELECT ?, ?, ?, ? WHERE {guard}",
                                   (novel_id, p.id, category, value, novel_id, revision))
                for position, fact in enumerate(p.facts):
                    tx.execute(f'INSERT INTO xh_rule_facts(novel_id,profile_id,position,text) SELECT ?,?,?,? WHERE {guard}',
                               (novel_id,p.id,position,fact,novel_id,revision))
        def committed():
            actual = self.get_rules(novel_id)
            if actual.revision == revision:
                return actual
            if actual.revision not in ("", rules.revision):
                raise ReviewConflict("设定已在别处更新，请刷新后再保存")
            return None
        return self._wait(committed)

    def find_run(self, novel_id: str, chapter_id: str, content_hash: str, rules_hash: str, engine_version: str = ENGINE_VERSION) -> dict | None:
        row = self.db.fetch_one(
            """SELECT id FROM xh_review_runs WHERE novel_id = ? AND chapter_id = ?
               AND content_hash = ? AND rules_hash = ? AND engine_version = ?""",
            (novel_id, chapter_id, content_hash, rules_hash, engine_version),
        )
        return self.get_run(novel_id, row["id"]) if row else None

    def save_run(self, run: dict) -> dict:
        fields = ("id", "novel_id", "chapter_id", "chapter_number", "content_hash", "content_snapshot",
                  "rules_hash", "engine_version", "created_at", "elapsed_ms")
        issue_fields = ("id", "kind", "entity_id", "entity_name", "action", "start", "end", "quote",
                        "source_id", "source_quote", "source_hash", "description")
        extra_run = {"mode": "rules", "context_hash": "", "model_name": "", "input_tokens": None,
                     "output_tokens": None, "model_calls": 0, "summary": "", "retrieval_mode": ""}
        extra_issue = {"source_kind": "setting", "source_chapter_id": None, "source_chapter_number": None,
                       "source_start": None, "source_end": None, "source_content_hash": "", "origin": "rules"}
        fields = (*fields, *extra_run)
        issue_fields = (*issue_fields, *extra_issue)
        run = {**extra_run, **run}
        with self.db.transaction() as tx:
            tx.execute(f"INSERT OR IGNORE INTO xh_review_runs ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
                       tuple(run[f] for f in fields))
            for issue in [*run["issues"], *run["uncertain"]]:
                issue = {**extra_issue, **issue}
                tx.execute(
                    f"INSERT INTO xh_review_issues (run_id, {', '.join(issue_fields)}) SELECT {', '.join('?' for _ in range(len(issue_fields) + 1))} "
                    "WHERE EXISTS (SELECT 1 FROM xh_review_runs WHERE id = ?)",
                    (run["id"], *(issue[f] for f in issue_fields), run["id"]),
                )
            for warning in dict.fromkeys(run.get("warnings", [])):
                tx.execute("INSERT INTO xh_review_warnings(run_id, message) SELECT ?, ? WHERE EXISTS (SELECT 1 FROM xh_review_runs WHERE id = ?)",
                           (run["id"], warning, run["id"]))
            if run.get('retrieval_usage') is not None:
                tx.execute('INSERT OR REPLACE INTO xh_review_retrieval_usage(run_id,usage_json) VALUES (?,?)',
                           (run['id'], json.dumps(run['retrieval_usage'], allow_nan=False)))
            for source in run.get("sources", []):
                tx.execute("""INSERT INTO xh_review_sources(run_id, id, chapter_id, chapter_number, start, end, text, content_hash)
                    SELECT ?, ?, ?, ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM xh_review_runs WHERE id = ?)""",
                           (run["id"], *(source[f] for f in ("id", "chapter_id", "chapter_number", "start", "end", "text", "content_hash")), run["id"]))
        return self._wait(lambda: self.find_run(run["novel_id"], run["chapter_id"], run["content_hash"], run["rules_hash"], run["engine_version"]))

    def get_run(self, novel_id: str, run_id: str) -> dict:
        row = self.db.fetch_one("SELECT * FROM xh_review_runs WHERE novel_id = ? AND id = ?", (novel_id, run_id))
        if row is None:
            raise ReviewNotFound("未找到该作品中的审校记录")
        findings = self.db.fetch_all(
            """SELECT id, kind, entity_id, entity_name, action, start, end, quote, source_id,
               source_quote, source_hash, description, status, feedback_version,
               source_kind, source_chapter_id, source_chapter_number, source_start, source_end, source_content_hash, origin
               FROM xh_review_issues WHERE run_id = ? ORDER BY start, end, id""", (run_id,),
        )
        warnings = self.db.fetch_all("SELECT message FROM xh_review_warnings WHERE run_id = ? ORDER BY rowid", (run_id,))
        sources = self.db.fetch_all("SELECT id, chapter_id, chapter_number, start, end, text, content_hash FROM xh_review_sources WHERE run_id = ? ORDER BY rowid", (run_id,))
        usage = self.db.fetch_one('SELECT usage_json FROM xh_review_retrieval_usage WHERE run_id=?', (run_id,))
        return {**row, "warnings": [w["message"] for w in warnings], "sources": sources,
                'retrieval_usage': json.loads(usage['usage_json']) if usage else {},
                "issues": [i for i in findings if i["kind"] == "conflict"],
                "uncertain": [i for i in findings if i["kind"] == "uncertain"]}

    def latest_run(self, novel_id: str, chapter_id: str, mode: str = "rules") -> dict | None:
        row = self.db.fetch_one(
            "SELECT id FROM xh_review_runs WHERE novel_id = ? AND chapter_id = ? AND mode = ? ORDER BY rowid DESC LIMIT 1",
            (novel_id, chapter_id, mode),
        )
        return self.get_run(novel_id, row["id"]) if row else None

    def feedback(self, novel_id: str, run_id: str, issue_id: str, status: str, version: int) -> None:
        self.get_run(novel_id, run_id)  # Authorize the owning work before writing.
        token = uuid4().hex
        with self.db.transaction() as tx:
            tx.execute(
                """UPDATE xh_review_issues SET status = ?, feedback_version = feedback_version + 1,
                   feedback_token = ? WHERE run_id = ? AND id = ? AND kind = 'conflict' AND feedback_version = ?
                   AND EXISTS (SELECT 1 FROM xh_review_runs r JOIN chapters c ON c.id = r.chapter_id AND c.novel_id = r.novel_id
                       JOIN xh_rule_sets s ON s.novel_id = r.novel_id WHERE r.id = xh_review_issues.run_id
                       AND c.content = r.content_snapshot AND c.number = r.chapter_number AND s.content_hash = r.rules_hash)
                   AND NOT EXISTS (SELECT 1 FROM xh_review_sources src LEFT JOIN chapters history
                       ON history.id = src.chapter_id AND history.novel_id = ? WHERE src.run_id = xh_review_issues.run_id
                       AND (history.id IS NULL OR history.number != src.chapter_number
                            OR substr(history.content, src.start + 1, src.end - src.start) != src.text))""",
                (status, token, run_id, issue_id, version, novel_id),
            )
            tx.execute(
                """INSERT INTO xh_feedback_events (id, run_id, issue_id, status, version, created_at)
                   SELECT ?, ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM xh_review_issues
                   WHERE run_id = ? AND id = ? AND feedback_token = ?)""",
                (token, run_id, issue_id, status, version + 1, datetime.now(timezone.utc).isoformat(), run_id, issue_id, token),
            )
        def committed():
            row = self.db.fetch_one("SELECT feedback_version, feedback_token FROM xh_review_issues WHERE run_id = ? AND id = ?", (run_id, issue_id))
            if row is None:
                raise ReviewNotFound("审校问题已不存在")
            if row["feedback_token"] == token:
                return True
            if row["feedback_version"] != version:
                raise ReviewConflict("问题已在别处处理，请刷新后重试")
            fresh = self.db.fetch_one(
                """SELECT c.content = r.content_snapshot AND c.number = r.chapter_number
                   AND s.content_hash = r.rules_hash AS current FROM xh_review_runs r
                   JOIN chapters c ON c.id = r.chapter_id AND c.novel_id = r.novel_id
                   JOIN xh_rule_sets s ON s.novel_id = r.novel_id WHERE r.id = ?""", (run_id,),
            )
            if not fresh or not fresh["current"]:
                raise ReviewConflict("正文或设定已变化，请重新审校")
            return None
        self._wait(committed)

    def _wait(self, check):
        deadline = monotonic() + self.write_timeout
        while True:
            value = check()
            if value is not None:
                return value
            if monotonic() >= deadline:
                raise ReviewUnavailable("尚未确认写入完成，请刷新检查结果后再重试")
            sleep(0.02)

    def history_rows(self, novel_id: str, number: int) -> list[dict]:
        return self.db.fetch_all(
            "SELECT id, novel_id, number, content FROM chapters WHERE novel_id = ? AND number < ? ORDER BY number DESC LIMIT 100",
            (novel_id, number),
        )

    def update_history_index(self, rows: list[dict], chunks: list[dict]) -> None:
        from application.audit.services.history_retrieval import INDEX_VERSION
        changed = []
        for row in rows:
            state = self.db.fetch_one("SELECT content_hash, index_version FROM xh_history_index WHERE chapter_id = ? AND novel_id = ?", (row["id"], row["novel_id"]))
            if not state or state["content_hash"] != row["content_hash"] or state["index_version"] != INDEX_VERSION:
                changed.append(row)
        if not changed:
            return
        changed_ids = {r["id"] for r in changed}
        with self.db.transaction() as tx:
            for row in changed:
                tx.execute("DELETE FROM xh_history_chunks WHERE chapter_id = ? AND novel_id = ?", (row["id"], row["novel_id"]))
                tx.execute("INSERT OR REPLACE INTO xh_history_index(chapter_id, novel_id, content_hash, index_version) VALUES (?, ?, ?, ?)",
                           (row["id"], row["novel_id"], row["content_hash"], INDEX_VERSION))
            for chunk in chunks:
                if chunk["chapter_id"] in changed_ids:
                    tx.execute("""INSERT OR REPLACE INTO xh_history_chunks(id, chapter_id, novel_id, chapter_number, start, end, text, content_hash)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                               tuple(chunk[k] for k in ("id", "chapter_id", "novel_id", "chapter_number", "start", "end", "text", "content_hash")))
        def committed():
            return True if all(self.db.fetch_one("SELECT chapter_id FROM xh_history_index WHERE chapter_id = ? AND content_hash = ? AND index_version = ?",
                (r["id"], r["content_hash"], INDEX_VERSION)) for r in changed) else None
        self._wait(committed)

    def get_embedding(self, key: str):
        row = self.db.fetch_one("SELECT vector_json FROM xh_embedding_cache WHERE cache_key = ?", (key,))
        return json.loads(row["vector_json"]) if row else None

    def save_embeddings(self, vectors: dict) -> None:
        with self.db.transaction() as tx:
            for key, vector in vectors.items():
                tx.execute("INSERT OR REPLACE INTO xh_embedding_cache(cache_key, vector_json) VALUES (?, ?)",
                           (key, json.dumps(vector, allow_nan=False)))
        self._wait(lambda: True if all(self.get_embedding(k) is not None for k in vectors) else None)

    def list_runs(self, novel_id: str, chapter_id: str, limit: int = 20) -> list[dict]:
        return self.db.fetch_all("""SELECT id, mode, model_name, created_at, input_tokens, output_tokens, elapsed_ms
            FROM xh_review_runs WHERE novel_id = ? AND chapter_id = ? ORDER BY rowid DESC LIMIT ?""",
            (novel_id, chapter_id, limit))

    def character_rows(self, novel_id: str) -> list[dict]:
        self.require_novel(novel_id)
        return self.db.fetch_all('SELECT id,name,description,gender,age,appearance,personality,background,core_belief '
                                 'FROM unified_characters WHERE novel_id=? ORDER BY id LIMIT 65', (novel_id,))
