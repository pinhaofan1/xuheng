"""Book-scoped vector cache and durable admission for background batches."""
import json
import math
from uuid import uuid4

from application.audit.services.history_retrieval import INDEX_VERSION


_CHUNKS = """FROM xh_search_chunks c
    JOIN xh_search_documents d ON d.chapter_id=c.chapter_id
    LEFT JOIN xh_search_vectors v ON v.novel_id=d.novel_id AND v.text=c.text AND v.fingerprint=?
    WHERE d.novel_id=? AND d.index_version=?"""


class SearchVectorStore:
    def __init__(self, index):
        self.index = index

    def status(self, novel_id):
        with self.index.connection() as conn:
            job = conn.execute('SELECT * FROM xh_search_vector_jobs WHERE novel_id=?', (novel_id,)).fetchone()
            if not job:
                return None
            total, ready = conn.execute('SELECT count(*),count(v.dimensions) '+_CHUNKS,
                                       (job['fingerprint'], novel_id, INDEX_VERSION)).fetchone()
            return {'id': job['id'], 'status': job['status'], 'error': job['error'],
                    'fingerprint': job['fingerprint'], 'pending_confirmation': bool(job['claim']),
                    'indexed_chunks': ready, 'total_chunks': total}

    def enqueue(self, novel_id, fingerprint):
        with self.index.connection(write=True) as conn:
            self.index.require_novel(conn, novel_id)
            current = conn.execute('SELECT * FROM xh_search_vector_jobs WHERE novel_id=?', (novel_id,)).fetchone()
            if current and current['claim']:
                raise ValueError('上次嵌入请求尚未确认，请先核对服务用量')
            if current and current['status'] == 'queued' and current['fingerprint'] == fingerprint:
                return current['id']
            job_id = uuid4().hex
            conn.execute('DELETE FROM xh_search_vectors WHERE novel_id=? AND fingerprint<>?', (novel_id, fingerprint))
            conn.execute("""INSERT OR REPLACE INTO xh_search_vector_jobs(novel_id,id,fingerprint,status)
                            VALUES (?,?,?,'queued')""", (novel_id, job_id, fingerprint))
            return job_id

    def cancel(self, novel_id, job_id):
        with self.index.connection(write=True) as conn:
            return bool(conn.execute("""UPDATE xh_search_vector_jobs SET status='cancelled',updated_at=CURRENT_TIMESTAMP
                                      WHERE novel_id=? AND id=? AND status='queued'""", (novel_id, job_id)).rowcount)

    def pending(self):
        with self.index.connection() as conn:
            return conn.execute("""SELECT * FROM xh_search_vector_jobs WHERE status='queued' AND claim=''
                                 ORDER BY updated_at,novel_id LIMIT 1""").fetchone()

    def claim(self, job, fingerprint):
        with self.index.connection(write=True) as conn:
            current = conn.execute('SELECT * FROM xh_search_vector_jobs WHERE id=?', (job['id'],)).fetchone()
            if current != job:
                return None, []
            if job['fingerprint'] != fingerprint:
                conn.execute("""UPDATE xh_search_vector_jobs SET status='failed',error='嵌入配置已变化，请重新构建'
                              WHERE id=?""", (job['id'],))
                return None, []
            rows = conn.execute('SELECT DISTINCT c.text '+_CHUNKS+' AND v.dimensions IS NULL LIMIT 32',
                                (fingerprint, job['novel_id'], INDEX_VERSION)).fetchall()
            if not rows:
                if self.index.pending_chapter(conn, job['novel_id']) is None:
                    conn.execute("UPDATE xh_search_vector_jobs SET status='completed' WHERE id=?", (job['id'],))
                return None, []
            token = uuid4().hex
            conn.execute('UPDATE xh_search_vector_jobs SET claim=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                         (token, job['id']))
            return token, [row[0] for row in rows]

    def fail(self, job, token, *, unknown):
        with self.index.connection(write=True) as conn:
            message = '嵌入结果待核对，已停止自动请求' if unknown else '嵌入请求未发送，请检查配置或作品预算'
            conn.execute("""UPDATE xh_search_vector_jobs SET status='failed',claim=?,error=?,updated_at=CURRENT_TIMESTAMP
                          WHERE id=? AND claim=?""", (token if unknown else '', message, job['id'], token))

    def publish(self, job, token, texts, vectors):
        normalized = [unit_vector(vector) for vector in vectors]
        dimensions = {len(vector) for vector in normalized}
        if len(normalized) != len(texts) or len(dimensions) != 1:
            raise ValueError('嵌入数量或维度不一致')
        dimension = dimensions.pop()
        args = (job['novel_id'], job['fingerprint'])
        with self.index.connection(write=True) as conn:
            if not conn.execute('SELECT 1 FROM xh_search_vector_jobs WHERE id=? AND claim=?',
                                (job['id'], token)).fetchone():
                return
            existing = conn.execute('SELECT dimensions FROM xh_search_vectors WHERE novel_id=? AND fingerprint=? LIMIT 1',
                                    args).fetchone()
            if existing and existing[0] != dimension:
                raise ValueError('嵌入服务返回维度发生变化')
            # Join current documents again after the request; edited or deleted text
            # cannot reappear through a late model response.
            live = {row[0] for row in conn.execute(
                'SELECT DISTINCT c.text '+_CHUNKS+' AND c.text IN ('+','.join('?' for _ in texts)+')',
                (job['fingerprint'], job['novel_id'], INDEX_VERSION, *texts))}
            conn.executemany('INSERT OR IGNORE INTO xh_search_vectors VALUES (?,?,?,?,?)',
                             [(*args, text, dimension, json.dumps(vector, allow_nan=False))
                              for text, vector in zip(texts, normalized) if text in live])
            conn.execute("UPDATE xh_search_vector_jobs SET claim='',error='',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                         (job['id'],))

    def history(self, novel_id, before, fingerprint):
        with self.index.connection() as conn:
            digest, indexed, total = self.index.history_scope(conn, novel_id, before)
            return digest, indexed, total, conn.execute('''SELECT c.id,c.chapter_id,c.start,c.end,c.text,d.novel_id,
                d.number AS chapter_number,d.content_hash,v.vector_json,v.dimensions '''+_CHUNKS+' AND d.number<?',
                (fingerprint, novel_id, INDEX_VERSION, before)).fetchall()


def unit_vector(vector):
    if not vector or not all(isinstance(value, (float, int)) and not isinstance(value, bool)
                             and math.isfinite(value) for value in vector):
        raise ValueError('嵌入向量无效')
    norm = math.hypot(*vector)
    if not norm or not math.isfinite(norm):
        raise ValueError('嵌入向量长度无效')
    return [value/norm for value in vector]
