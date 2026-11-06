"""Author decisions and exact manuscript evidence, scoped to one work."""
from contextlib import contextmanager
import json

from pydantic import ValidationError

from application.audit.evidence_models import text_hash
from application.audit.fact_models import ExtractedFact
from application.audit.services.evidence_review_service import ReviewConflict, ReviewNotFound
from infrastructure.persistence.database.atomic_effect import binding_for


_SOURCES = """SELECT f.*,c.number AS current_number,c.content AS current_content
    FROM xh_fact_candidates f LEFT JOIN chapters c ON c.novel_id=f.novel_id AND c.id=f.source_chapter_id"""
_STATE = ('status', 'version', 'profile_id', 'replaces', 'applies_from', 'applies_through')


class FactStore:
    def __init__(self, db):
        self.db = db

    @contextmanager
    def transaction(self, *, write=False):
        bound = binding_for(self.db.db_path)
        if bound is not None:
            with bound:
                yield bound
            return
        conn = self.db.get_connection()
        with conn:
            conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield conn

    @staticmethod
    def source_valid(row):
        if row['source_number'] != row['current_number']:
            return False
        content = row['current_content'] or ''
        return (content[row['source_start']:row['source_end']] == row['source_quote']
                and text_hash(content) == row['source_hash'])

    def add_extracted(self, novel_id, number, content, extracted):
        content_hash = text_hash(content)
        with self.transaction(write=True) as conn:
            chapter = conn.execute('SELECT id,content FROM chapters WHERE novel_id=? AND number=?',
                                   (novel_id, number)).fetchone()
            if not chapter or chapter['content'] != content:
                return 0
            saved = 0
            for item in extracted[:5]:
                try:
                    fact = ExtractedFact.model_validate(item)
                except ValidationError:
                    continue
                start = content.find(fact.quote)
                if start < 0 or content.find(fact.quote, start+1) >= 0 or '\x00' in fact.assertion:
                    continue
                identifier = text_hash(json.dumps(
                    [novel_id, chapter['id'], content_hash, number, start, fact.character_name, fact.assertion],
                    ensure_ascii=False))
                saved += conn.execute("""INSERT OR IGNORE INTO xh_fact_candidates
                    (id,novel_id,source_chapter_id,source_number,source_hash,source_start,source_end,source_quote,subject_name,assertion)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""", (identifier, novel_id, chapter['id'], number, content_hash,
                    start, start+len(fact.quote), fact.quote, fact.character_name, fact.assertion)).rowcount
            return saved

    @staticmethod
    def revision(conn, novel_id):
        row = conn.execute('SELECT revision FROM xh_rule_sets WHERE novel_id=?', (novel_id,)).fetchone()
        return row['revision'] if row else ''

    def list_facts(self, novel_id, *, status=None, offset=0, limit=30):
        with self.transaction() as conn:
            if not conn.execute('SELECT 1 FROM novels WHERE id=?', (novel_id,)).fetchone():
                raise ReviewNotFound('未找到作品')
            condition = ' WHERE f.novel_id=?'
            params = (novel_id,)
            if status is not None:
                condition += ' AND f.status=?'
                params += (status,)
            rows = conn.execute(_SOURCES+condition+' ORDER BY f.source_number DESC,f.id LIMIT ? OFFSET ?',
                                (*params, limit+1, offset)).fetchall()
            return {'facts': [self.public(row) for row in rows[:limit]],
                    'has_more': len(rows) > limit, 'rules_revision': self.revision(conn, novel_id)}

    def public(self, row):
        fact = dict(row)
        fact['source_valid'] = self.source_valid(row)
        fact.pop('current_content')
        fact.pop('current_number')
        return fact

    def history(self, novel_id, fact_id):
        with self.transaction() as conn:
            if not conn.execute('SELECT 1 FROM xh_fact_candidates WHERE novel_id=? AND id=?',
                                (novel_id, fact_id)).fetchone():
                raise ReviewNotFound('未找到该作品的事实')
            rows = conn.execute("""SELECT before_json,after_json,request_json,created_at FROM xh_fact_decisions
                WHERE novel_id=? AND fact_id=? ORDER BY rowid DESC LIMIT 100""", (novel_id, fact_id))
            return [{'before': json.loads(r['before_json']), 'after': json.loads(r['after_json']),
                     'action': json.loads(r['request_json'])['action'], 'created_at': r['created_at']} for r in rows]

    def compare(self, novel_id, fact_id, profile_id):
        with self.transaction() as conn:
            if not conn.execute('SELECT 1 FROM xh_fact_candidates WHERE novel_id=? AND id=?', (novel_id, fact_id)).fetchone():
                raise ReviewNotFound('未找到该作品的事实')
            profile = conn.execute('SELECT name FROM xh_rule_profiles WHERE novel_id=? AND id=?',
                                   (novel_id, profile_id)).fetchone()
            if not profile:
                raise ReviewNotFound('未找到该作品的人物')
            settings = conn.execute('SELECT text FROM xh_rule_facts WHERE novel_id=? AND profile_id=? ORDER BY position',
                                    (novel_id, profile_id))
            active = conn.execute(_SOURCES+""" WHERE f.novel_id=? AND f.profile_id=? AND f.id<>?
                AND f.status IN ('accept','coexist') ORDER BY f.applies_from,f.id""", (novel_id, profile_id, fact_id))
            return {'profile_name': profile['name'], 'settings': [r['text'] for r in settings],
                    'related': [self.public(row) for row in active if self.source_valid(row)]}

    def decide(self, novel_id, fact_id, request):
        payload = request.model_dump_json()
        with self.transaction(write=True) as conn:
            receipt = conn.execute('SELECT * FROM xh_fact_decisions WHERE novel_id=? AND request_id=?',
                                   (novel_id, request.request_id)).fetchone()
            if receipt:
                if receipt['fact_id'] != fact_id or receipt['request_json'] != payload:
                    raise ReviewConflict('请求编号已用于另一项操作')
                return json.loads(receipt['after_json'])
            fact = conn.execute(_SOURCES+' WHERE f.novel_id=? AND f.id=?', (novel_id, fact_id)).fetchone()
            if not fact:
                raise ReviewNotFound('未找到该作品的事实')
            revision = self.revision(conn, novel_id)
            if revision != request.rules_revision or fact['version'] != request.expected_version:
                raise ReviewConflict('事实或人物设定已变化，请刷新后核对差异')
            before = {key: fact[key] for key in _STATE}
            if request.action == 'undo':
                if fact['status'] == 'pending':
                    raise ReviewConflict('这条候选还没有可撤销的处理')
                last = conn.execute("""SELECT before_json FROM xh_fact_decisions
                    WHERE novel_id=? AND fact_id=? ORDER BY rowid DESC LIMIT 1""", (novel_id, fact_id)).fetchone()
                if not last:
                    raise ReviewConflict('缺少可撤销的处理记录')
                # Only this candidate's overlay is restored; authored settings
                # and other candidates are never rewritten by undo.
                after = json.loads(last['before_json'])
            else:
                if fact['status'] != 'pending':
                    raise ReviewConflict('请先撤销已有处理')
                after = self._resolve(conn, novel_id, fact, request)
            after['version'] = fact['version']+1
            conn.execute("""UPDATE xh_fact_candidates SET status=?,version=?,profile_id=?,replaces=?,applies_from=?,applies_through=?
                WHERE novel_id=? AND id=?""", (*(after[key] for key in _STATE), novel_id, fact_id))
            conn.execute("""INSERT INTO xh_fact_decisions(novel_id,request_id,fact_id,request_json,before_json,after_json,rules_revision)
                VALUES (?,?,?,?,?,?,?)""", (novel_id, request.request_id, fact_id, payload,
                json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), revision))
            return after

    def _resolve(self, conn, novel_id, fact, request):
        state = dict.fromkeys(_STATE)
        state['status'] = request.action
        if request.action == 'reject':
            return state
        if not self.source_valid(fact):
            raise ReviewConflict('来源正文或章节编号已变化，请重新提取后再确认')
        if not conn.execute('SELECT 1 FROM xh_rule_profiles WHERE novel_id=? AND id=?',
                            (novel_id, request.profile_id)).fetchone():
            raise ValueError('请选择本作品已保存的审校人物')
        start = request.applies_from if request.applies_from is not None else fact['source_number']+1
        end = request.applies_through
        if start <= fact['source_number'] or (end is not None and end < start):
            raise ValueError('适用章节须在来源章节之后，且结束章节不能早于起始章节')
        if start > 100_000:
            raise ValueError('适用章节不能超过 100000')
        if request.replaces is not None:
            if request.action != 'accept' or not conn.execute("""SELECT 1 FROM xh_rule_facts
                WHERE novel_id=? AND profile_id=? AND text=?""", (novel_id, request.profile_id, request.replaces)).fetchone():
                raise ReviewConflict('要替换的设定已变化，或当前处理方式不支持替换')
            overlap = conn.execute(_SOURCES+""" WHERE f.novel_id=? AND f.profile_id=? AND f.replaces=?
                AND f.status='accept' AND f.applies_from<=coalesce(?,100000) AND coalesce(f.applies_through,100000)>=?""",
                (novel_id, request.profile_id, request.replaces, end, start))
            if any(self.source_valid(other) for other in overlap):
                raise ReviewConflict('适用范围内已有对同一设定的替换，请先处理那条事实')
        state.update(profile_id=request.profile_id, replaces=request.replaces, applies_from=start, applies_through=end)
        return state

    def applicable(self, conn, novel_id, number):
        rows = conn.execute(_SOURCES+""" WHERE f.novel_id=:novel_id AND f.status IN ('accept','coexist')
            AND f.applies_from<=:number AND (f.applies_through IS NULL OR f.applies_through>=:number)
            AND f.source_number<:number ORDER BY f.source_number,f.id""", {'novel_id': novel_id, 'number': number})
        return [row for row in rows if self.source_valid(row)]
