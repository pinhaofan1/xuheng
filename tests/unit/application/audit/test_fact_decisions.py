import json
from contextlib import closing
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from application.audit.evidence_models import CharacterRule, RuleSet
from application.audit.fact_models import FactDecision
from application.audit.services.ai_evidence_review import validate_model_review
from application.audit.services.evidence_review_service import EvidenceReviewService, ReviewConflict
from application.world.services.chapter_narrative_sync import persist_fact_candidates
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.evidence_review_store import EvidenceReviewStore
from infrastructure.persistence.database.fact_store import FactStore
from infrastructure.persistence.backup_service import BackupService
from infrastructure.persistence.operation_journal import OperationJournal
from interfaces.api.v1.audit.evidence_review_routes import get_evidence_review_service, router


BASELINE = '林澈的左手可以持剑'
PAST = '🌊海风吹过。林澈的左臂已经断了，无法抬起。'
QUOTE = '林澈的左臂已经断了'
TARGET = '林澈挥剑斩断藤蔓。'


@pytest.fixture
def workspace(tmp_path):
    db = DatabaseConnection(str(tmp_path/'facts.db'))
    with db.get_connection() as conn:
        conn.executemany('INSERT INTO novels(id,title,slug) VALUES (?,?,?)', [('book','A','a'), ('other','B','b')])
        conn.executemany('INSERT INTO chapters(id,novel_id,number,content) VALUES (?,?,?,?)', [
            ('source','book',1,PAST), ('target','book',2,TARGET), ('later','book',3,TARGET), ('foreign','other',1,PAST),
        ])
    reviews = EvidenceReviewStore(db)
    for book in ('book', 'other'):
        reviews.save_rules(book, RuleSet(profiles=[
            CharacterRule(id='lin', name='林澈', facts=[BASELINE], forbidden_actions=['use_sword']),
        ]))
    yield db, reviews, FactStore(db)
    db.close_all()


def extracted(assertion='左臂骨折', quote=QUOTE):
    return {'character_name': '林澈', 'assertion': assertion, 'quote': quote}


def test_candidates_require_saved_source_and_are_reusable(workspace):
    db, reviews, store = workspace
    assert persist_fact_candidates(db, 'book', 1, PAST, [extracted(), extracted(quote='模型编造的原句')]) == 1
    assert persist_fact_candidates(db, 'book', 1, PAST, [extracted()]) == 0
    assert persist_fact_candidates(db, 'book', 1, PAST+'改稿', [extracted()]) == 0
    fact = store.list_facts('book')['facts'][0]
    assert fact['source_valid'] and PAST[fact['source_start']:fact['source_end']] == QUOTE
    assert fact['status'] == 'pending' and not reviews.review_context('book', 2).facts
    assert not store.list_facts('other')['facts']


def test_decision_scope_exact_ai_quote_and_immutable_report(workspace):
    db, reviews, store = workspace
    service = EvidenceReviewService(reviews)
    initial = service.review('book', 2)
    persist_fact_candidates(db, 'book', 1, PAST, [extracted()])
    fact = store.list_facts('book')['facts'][0]
    revision = reviews.get_rules('book').revision
    request = FactDecision(request_id='first', expected_version=0, rules_revision=revision, action='accept',
                           profile_id='lin', replaces=BASELINE, applies_from=2, applies_through=2)
    applied = store.decide('book', fact['id'], request)
    assert store.decide('book', fact['id'], request) == applied
    with pytest.raises(ReviewConflict, match='请求编号'):
        store.decide('book', fact['id'], request.model_copy(update={'action': 'reject'}))
    assert not reviews.review_context('book', 1).facts
    assert not reviews.review_context('book', 3).facts
    assert not reviews.review_context('other', 2).facts
    ctx = reviews.review_context('book', 2)
    assert not ctx.rules.profiles[0].facts and len(ctx.facts) == 1
    assert reviews.get_rules('book').profiles[0].facts == [BASELINE]
    reply = json.dumps({'issues': [{
        'entity_id': 'lin', 'issue_type': 'state', 'description': '检查左臂', 'target_quote': '挥剑斩断藤蔓',
        'source_id': ctx.facts[0]['id'], 'source_quote': QUOTE, 'certainty': 'uncertain',
    }]}, ensure_ascii=False)
    _, uncertain, warnings, _ = validate_model_review(TARGET, ctx.rules, list(ctx.facts), reply)
    assert not warnings and uncertain[0]['source_chapter_id'] == 'source'
    assert PAST[uncertain[0]['source_start']:uncertain[0]['source_end']] == QUOTE
    assert service.get_run('book', initial['id'])['stale']
    changed = service.review('book', 2)
    assert changed['sources'][0]['text'] == QUOTE
    store.decide('book', fact['id'], FactDecision(
        request_id='undo', expected_version=1, rules_revision=revision, action='undo'))
    assert service.review('book', 2)['id'] == initial['id']
    archived = service.get_run('book', changed['id'])
    assert archived['stale'] and archived['sources'][0]['text'] == QUOTE
    assert [event['action'] for event in store.history('book', fact['id'])] == ['undo', 'accept']


def test_http_conflicts_rejection_and_version_invalidation(workspace):
    db, reviews, store = workspace
    persist_fact_candidates(db, 'book', 1, PAST, [extracted(), extracted('左臂暂时不能用')])
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_evidence_review_service] = lambda: EvidenceReviewService(reviews)
    with TestClient(app) as client:
        base = '/evidence-reviews/book/facts'
        data = client.get(base).json()
        fact, second = data['facts']
        root = f"{base}/{fact['id']}"
        assert client.get(root+'/comparison', params={'profile_id': 'lin'}).json()['settings'] == [BASELINE]
        decision = {'request_id': 'coexist', 'expected_version': 0, 'rules_revision': data['rules_revision'],
                    'action': 'coexist', 'profile_id': 'lin', 'applies_from': 2}
        assert client.post(root.replace('/book/', '/other/')+'/decisions', json=decision).status_code == 404
        assert client.post(root+'/decisions', json={**decision, 'expected_version': 5}).status_code == 409
        assert client.post(root+'/decisions', json=decision).status_code == 200
        assert reviews.review_context('book', 2).rules.profiles[0].facts == [BASELINE]
        assert len(reviews.review_context('book', 2).facts) == 1
        with db.get_connection() as conn:
            conn.execute("UPDATE chapters SET content=content||'改稿' WHERE id='source'")
        assert not reviews.review_context('book', 2).facts
        second_path = f"{base}/{second['id']}/decisions"
        assert client.post(second_path, json={**decision, 'request_id': 'stale-source'}).status_code == 409
        assert client.post(second_path, json={**decision, 'action': 'reject', 'request_id': 'reject'}).status_code == 200
        assert client.get(base, params={'status': 'reject'}).json()['facts'][0]['source_valid'] is False


def test_paired_backup_before_legacy_schema_upgrade_preserves_report(workspace):
    db, reviews, _ = workspace
    report = EvidenceReviewService(reviews).review('book', 2)
    path = db.db_path
    db.close_all()
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('DROP TABLE xh_fact_decisions')
        conn.execute('DROP TABLE xh_fact_candidates')
        conn.execute("DELETE FROM migrations_applied WHERE migration_file='add_xuheng_fact_candidates.sql'")
    backup = BackupService(path)
    OperationJournal(backup.operations)
    manifest = backup.create()
    assert backup.inspect(backup.path(manifest['id']))['id'] == manifest['id']
    upgraded = DatabaseConnection(path)
    try:
        assert not FactStore(upgraded).list_facts('book')['facts']
        previous = EvidenceReviewService(EvidenceReviewStore(upgraded)).get_run('book', report['id'])
        assert previous['content_snapshot'] == report['content_snapshot']
        assert previous['issues'][0]['source_quote'] == report['issues'][0]['source_quote']
        assert not previous['stale']
    finally:
        upgraded.close_all()
