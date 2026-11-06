"""Durable lexical index. One chapter publishes in one SQLite transaction."""
from collections import Counter, defaultdict
from contextlib import closing, contextmanager, nullcontext
import json
import math
import sqlite3
from uuid import uuid4

from application.audit.evidence_models import text_hash
from application.audit.services.history_retrieval import INDEX_VERSION, split_chapter, terms


class SearchIndexStore:
    def __init__(self, db):
        self.path = db.db_path

    @contextmanager
    def connection(self, write=False):
        with closing(sqlite3.connect(self.path, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            with conn:
                conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
                yield conn

    def status(self, novel_id):
        with self.connection() as conn:
            self.require_novel(conn, novel_id)
            total = conn.execute('SELECT count(*) FROM chapters WHERE novel_id=?', (novel_id,)).fetchone()[0]
            indexed = conn.execute('SELECT count(*) FROM xh_search_documents WHERE novel_id=? AND index_version=?',
                                   (novel_id, INDEX_VERSION)).fetchone()[0]
            job = conn.execute('SELECT id,status,error,updated_at FROM xh_search_jobs WHERE novel_id=?', (novel_id,)).fetchone()
            return {'job': dict(job) if job else None, 'indexed_chapters': indexed, 'total_chapters': total}

    @staticmethod
    def require_novel(conn, novel_id):
        if not conn.execute('SELECT 1 FROM novels WHERE id=?', (novel_id,)).fetchone():
            raise KeyError(novel_id)

    def enqueue(self, novel_id, *, rebuild=False):
        with self.connection(write=True) as conn:
            self.require_novel(conn, novel_id)
            current = conn.execute('SELECT id,status FROM xh_search_jobs WHERE novel_id=?', (novel_id,)).fetchone()
            if current and current['status'] == 'queued' and not rebuild:
                return current['id']
            if rebuild:
                conn.execute('DELETE FROM xh_search_documents WHERE novel_id=?', (novel_id,))
            job_id = uuid4().hex
            conn.execute("INSERT OR REPLACE INTO xh_search_jobs(novel_id,id,status) VALUES (?,?,'queued')", (novel_id, job_id))
            return job_id

    def set_state(self, novel_id, job_id, *, resume=False):
        with self.connection(write=True) as conn:
            if resume:
                sql = "UPDATE xh_search_jobs SET status='queued',error='',updated_at=CURRENT_TIMESTAMP WHERE novel_id=? AND id=? AND status IN ('cancelled','failed')"
            else:
                sql = "UPDATE xh_search_jobs SET status='cancelled',updated_at=CURRENT_TIMESTAMP WHERE novel_id=? AND id=? AND status='queued'"
            return bool(conn.execute(sql, (novel_id, job_id)).rowcount)

    @staticmethod
    def pending_chapter(conn, novel_id):
        return conn.execute('''SELECT c.id,c.novel_id,c.number,coalesce(c.content,'') AS content FROM chapters c
            LEFT JOIN xh_search_documents d ON d.chapter_id=c.id AND d.index_version=?
            WHERE c.novel_id=? AND d.chapter_id IS NULL ORDER BY c.number,c.id LIMIT 1''', (INDEX_VERSION, novel_id)).fetchone()

    def process_next(self):
        with self.connection() as conn:
            job = conn.execute("SELECT novel_id,id FROM xh_search_jobs WHERE status='queued' ORDER BY updated_at,novel_id LIMIT 1").fetchone()
            if job is None:
                return False
            chapter = self.pending_chapter(conn, job['novel_id'])
        if chapter is None:
            with self.connection(write=True) as conn:
                if self.pending_chapter(conn, job['novel_id']) is None:
                    conn.execute("UPDATE xh_search_jobs SET status='completed',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='queued'", (job['id'],))
            return True
        try:
            row = dict(chapter)
            row['content_hash'] = text_hash(row['content'])
            chunks = [(chunk, terms(chunk['text'])) for chunk in split_chapter(row)]
            self.publish(job, chapter, row['content_hash'], chunks)
        except Exception:
            with self.connection(write=True) as conn:
                conn.execute("UPDATE xh_search_jobs SET status='failed',error='索引写入失败',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='queued'", (job['id'],))
            raise
        return True

    def publish(self, job, chapter, content_hash, chunks):
        with self.connection(write=True) as conn:
            active = conn.execute("SELECT 1 FROM xh_search_jobs WHERE id=? AND status='queued'", (job['id'],)).fetchone()
            if not active or self.pending_chapter(conn, job['novel_id']) != chapter:
                return
            conn.execute('DELETE FROM xh_search_documents WHERE chapter_id=?', (chapter['id'],))
            conn.execute('INSERT INTO xh_search_documents VALUES (?,?,?,?,?)',
                         (chapter['id'], job['novel_id'], chapter['number'], content_hash, INDEX_VERSION))
            conn.executemany('INSERT INTO xh_search_chunks VALUES (?,?,?,?,?,?)',
                             [(chunk['id'], chapter['id'], chunk['start'], chunk['end'], chunk['text'], sum(counts.values()))
                              for chunk, counts in chunks])
            conn.executemany('INSERT INTO xh_search_postings VALUES (?,?,?,?)',
                             [(chunk['id'], job['novel_id'], term, frequency)
                              for chunk, counts in chunks for term, frequency in counts.items()])

    @staticmethod
    def history_scope(conn, novel_id, before):
        rows = conn.execute('''SELECT c.id,c.number,d.content_hash FROM chapters c
            LEFT JOIN xh_search_documents d ON d.chapter_id=c.id AND d.index_version=?
            WHERE c.novel_id=? AND c.number<? ORDER BY c.number,c.id''', (INDEX_VERSION, novel_id, before)).fetchall()
        digest = 'index-v1:' + text_hash(json.dumps([tuple(row) for row in rows], ensure_ascii=False))
        return digest, sum(row['content_hash'] is not None for row in rows), len(rows)

    def context_digest(self, novel_id, before):
        with self.connection() as conn:
            return self.history_scope(conn, novel_id, before)[0]

    def lexical_context(self, novel_id, before, query):
        with self.connection() as conn:
            if not conn.execute('SELECT 1 FROM xh_search_jobs WHERE novel_id=?', (novel_id,)).fetchone():
                return None
            digest, indexed, total = self.history_scope(conn, novel_id, before)
            chunk_count = conn.execute('''SELECT count(*) FROM xh_search_chunks c
                JOIN xh_search_documents d ON d.chapter_id=c.chapter_id
                WHERE d.novel_id=? AND d.number<? AND d.index_version=?''', (novel_id, before, INDEX_VERSION)).fetchone()[0]
            return (self.lexical(novel_id, before, query, conn=conn), digest, indexed, total, chunk_count)

    def lexical(self, novel_id, before, query, limit=24, *, conn=None):
        query_terms = list(terms(query[:8000]))[:512]
        if not query_terms:
            return []
        scope = 'd.novel_id=? AND d.number<? AND d.index_version=?'
        params = (novel_id, before, INDEX_VERSION)
        with (self.connection() if conn is None else nullcontext(conn)) as conn:
            count, average = conn.execute('SELECT count(*),avg(c.term_count) FROM xh_search_chunks c '
                'JOIN xh_search_documents d ON d.chapter_id=c.chapter_id WHERE '+scope, params).fetchone()
            if not count:
                return []
            placeholders = ','.join('?' for _ in query_terms)
            hits = conn.execute('''SELECT p.term,p.frequency,c.id,c.term_count,d.number,c.start
                FROM xh_search_postings p JOIN xh_search_chunks c ON c.id=p.chunk_id
                JOIN xh_search_documents d ON d.chapter_id=c.chapter_id
                WHERE p.novel_id=? AND p.term IN ('''+placeholders+') AND '+scope,
                (novel_id, *query_terms, *params)).fetchall()
            frequency = Counter(hit['term'] for hit in hits)
            idf = {term: math.log1p((count-n+.5)/(n+.5)) for term, n in frequency.items()}
            scores = defaultdict(float)
            positions = {}
            for hit in hits:
                chunk_id, tf = hit['id'], hit['frequency']
                scores[chunk_id] += idf[hit['term']]*tf*2.2/(tf+1.2*(.25+.75*hit['term_count']/(average or 1)))
                positions[chunk_id] = (-hit['number'], hit['start'])
            ranked = sorted(scores, key=lambda key: (-scores[key], positions[key], key))[:limit]
            return [dict(conn.execute('''SELECT c.id,c.chapter_id,c.start,c.end,c.text,d.novel_id,
                         d.number AS chapter_number,d.content_hash FROM xh_search_chunks c
                         JOIN xh_search_documents d ON d.chapter_id=c.chapter_id WHERE c.id=?''', (key,)).fetchone(),
                         score=round(scores[key],6)) for key in ranked]
