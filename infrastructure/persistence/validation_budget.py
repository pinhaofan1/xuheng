"""Optional immutable spending envelope over the existing usage ledger.

Only explicitly enrolled validation books participate. Ordinary works keep their
existing policies. No second cost ledger: memberships point to original calls.
"""
import json
import math


def create_validation_budget_schema(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS validation_budgets(id TEXT PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS validation_budget_books(
            novel_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, stage TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS validation_budget_calls(
            call_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, stage TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS validation_calls_group ON validation_budget_calls(group_id,stage);
        CREATE TABLE IF NOT EXISTS validation_price_events(
            id INTEGER PRIMARY KEY, group_id TEXT NOT NULL, previous_body TEXT NOT NULL,
            next_body TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    ''')


def denied(message):
    from infrastructure.persistence.operation_journal import BudgetExceeded
    raise BudgetExceeded(message)


class ValidationBudget:
    def register_validation_budget(self, id, body, *, refresh_prices=False):
        if not isinstance(id,str) or not 1 <= len(id) <= 100:
            raise ValueError('验收预算标识无效')
        limit = body.get('limit')
        stages = body.get('stages')
        if type(limit) not in (int,float) or not math.isfinite(limit) or not 0 < limit <= 20:
            raise ValueError('本次验收总预算必须大于0且不超过20元')
        if not isinstance(stages,dict) or not stages or any(not isinstance(k,str) or not k or type(v) not in (int,float) or not math.isfinite(v) or v <= 0 for k,v in stages.items()) or sum(stages.values()) > limit:
            raise ValueError('阶段预算必须有效且合计不超过总预算')
        prices=body.get('prices')
        if not isinstance(prices,list) or not prices or body.get('currency') != 'CNY':
            raise ValueError('必须提供人民币价格和核对依据')
        seen=set()
        for price in prices:
            key=(price.get('kind'),price.get('model'))
            if key[0] not in {'llm','embedding','rerank'} or not isinstance(key[1],str) or not key[1] or key in seen:
                raise ValueError('模型价格配置无效或重复')
            seen.add(key)
            if not isinstance(price.get('source'),str) or not price['source'].startswith('https://') or not price.get('checked_at'):
                raise ValueError('缺少模型官方价格核对记录')
            for field in ('input_per_million','output_per_million'):
                if type(price.get(field)) not in (int,float) or not math.isfinite(price[field]) or price[field] < 0:
                    raise ValueError('模型单价无效')
        payload=json.dumps(body,sort_keys=True,ensure_ascii=False,allow_nan=False)
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            existing=conn.execute('SELECT body FROM validation_budgets WHERE id=?',(id,)).fetchone()
            if existing and existing[0] != payload:
                old=json.loads(existing[0])
                old_keys={(p['kind'],p['model']) for p in old['prices']}
                if not refresh_prices or {k:v for k,v in old.items() if k!='prices'} != {k:v for k,v in body.items() if k!='prices'} or not old_keys<=seen:
                    raise ValueError('验收预算已经固定，不允许覆盖或重置；请保留原账本')
                conn.execute('INSERT INTO validation_price_events(group_id,previous_body,next_body) VALUES (?,?,?)',(id,existing[0],payload))
                conn.execute('UPDATE validation_budgets SET body=? WHERE id=?',(payload,id))
            conn.execute('INSERT OR IGNORE INTO validation_budgets VALUES (?,?)',(id,payload))

    def validation_budget_config(self, group_id):
        with self.connection() as conn:
            row=conn.execute('SELECT body FROM validation_budgets WHERE id=?',(group_id,)).fetchone()
            if row is None:
                raise KeyError('验收预算不存在')
            return json.loads(row[0])

    def enroll_validation_book(self, novel_id, group_id, stage):
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT body FROM validation_budgets WHERE id=?',(group_id,)).fetchone()
            if row is None or stage not in json.loads(row[0])['stages']:
                raise ValueError('验收预算或阶段不存在')
            previous=conn.execute('SELECT group_id,stage FROM validation_budget_books WHERE novel_id=?',(novel_id,)).fetchone()
            if previous and tuple(previous) != (group_id,stage):
                raise ValueError('作品已绑定验收阶段，不允许换组或转移费用')
            if not previous and conn.execute('SELECT 1 FROM calls WHERE novel_id=? LIMIT 1',(novel_id,)).fetchone():
                raise ValueError('请在首次请求前绑定验收预算')
            conn.execute('INSERT OR IGNORE INTO validation_budget_books VALUES (?,?,?)',(novel_id,group_id,stage))

    def assert_validation_book(self, novel_id):
        with self.connection() as conn:
            if not conn.execute('SELECT 1 FROM validation_budget_books WHERE novel_id=?',(novel_id,)).fetchone():
                denied('隔离验收请求没有绑定累计预算，已在发送前停止')

    @staticmethod
    def _validation_totals(conn, group_id, stage=None):
        row=conn.execute('''SELECT count(*) AS calls,
            coalesce(sum(coalesce(u.cost,u.reserved_cost,0)),0) AS charged_cost,
            coalesce(sum(CASE WHEN u.cost IS NULL AND u.reserved_cost IS NULL THEN 1 ELSE 0 END),0) AS unpriced,
            coalesce(sum(CASE WHEN u.input_tokens IS NULL OR u.output_tokens IS NULL THEN 1 ELSE 0 END),0) AS unsettled,
            coalesce(sum(CASE WHEN u.status!='running' AND (u.input_tokens IS NULL OR u.output_tokens IS NULL) THEN 1 ELSE 0 END),0) AS unknown_finished
            FROM validation_budget_calls b JOIN usage_calls u ON u.id=b.call_id
            WHERE b.group_id=?'''+(' AND b.stage=?' if stage else ''),
            (group_id,stage) if stage else (group_id,)).fetchone()
        return dict(row)

    def validation_budget_summary(self, group_id):
        with self.connection() as conn:
            conn.execute('BEGIN')
            row=conn.execute('SELECT body FROM validation_budgets WHERE id=?',(group_id,)).fetchone()
            if row is None:
                raise KeyError('验收预算不存在')
            body=json.loads(row[0])
            return {'id':group_id,'limit':body['limit'],'totals':self._validation_totals(conn,group_id),
                    'stages':{stage:{'limit':limit,**self._validation_totals(conn,group_id,stage)} for stage,limit in body['stages'].items()}}

    def _validation_admission(self, conn, novel_id, kind, model, input_bound, output_bound):
        member=conn.execute('''SELECT b.group_id,b.stage,g.body FROM validation_budget_books b
            JOIN validation_budgets g ON g.id=b.group_id WHERE b.novel_id=?''',(novel_id,)).fetchone()
        if member is None:
            return None
        group_id,stage,payload=member
        body=json.loads(payload)
        price=next((p for p in body['prices'] if p['kind']==kind and p['model']==model),None)
        if price is None:
            denied('验收模型缺少已核对单价，已在发送前停止')
        total=self._validation_totals(conn,group_id)
        subtotal=self._validation_totals(conn,group_id,stage)
        if total['unpriced'] or total['unknown_finished']:
            denied('验收有请求费用或结果待确认，不自动重发，请先核对账单')
        reserve=(input_bound*price['input_per_million']+output_bound*price['output_per_million'])/1_000_000
        if total['charged_cost']+reserve > body['limit'] or subtotal['charged_cost']+reserve > body['stages'][stage]:
            denied('本次请求将超过验收累计或阶段金额预算，已在发送前停止')
        return group_id,stage,price
