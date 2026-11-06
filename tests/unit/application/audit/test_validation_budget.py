from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from infrastructure.persistence.operation_journal import OperationJournal, BudgetExceeded
from application.ai.usage_accounting import ModelCall, usage_scope


def policy():
    return {'limit':2,'currency':'CNY','stages':{'calibration':1,'longrun':1},
            'prices':[{'kind':'llm','model':'fixture','input_per_million':10,'output_per_million':10,
                       'source':'https://example.test/test-only-price','checked_at':'2026-11-05'}]}


@pytest.fixture
def journal(tmp_path):
    value=OperationJournal(tmp_path/'operations.sqlite3')
    value.register_validation_budget('goal',policy())
    value.enroll_validation_book('book-a','goal','calibration')
    value.enroll_validation_book('book-b','goal','calibration')
    value.enroll_validation_book('book-c','goal','longrun')
    return value


def test_stage_budget_combines_books_and_survives_reopen(journal):
    first=journal.reserve('book-a','llm','fixture',60000,0)
    journal.finish(first,60000,0)
    reopened=OperationJournal(journal.path)
    with pytest.raises(BudgetExceeded,match='阶段'):
        reopened.reserve('book-b','llm','fixture',60000,0)
    second=reopened.reserve('book-c','llm','fixture',60000,0)
    reopened.finish(second,60000,0)
    summary=reopened.validation_budget_summary('goal')
    assert summary['totals']['calls'] == 2
    assert summary['totals']['charged_cost'] == pytest.approx(1.2)
    assert summary['stages']['calibration']['charged_cost'] == pytest.approx(.6)


def test_reservations_are_atomic_under_concurrency(journal):
    def request(book):
        try:
            return journal.reserve(book,'llm','fixture',60000,0)
        except BudgetExceeded:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls=list(pool.map(request,['book-a','book-b']))
    assert len([call for call in calls if call]) == 1
    assert journal.validation_budget_summary('goal')['totals']['unsettled'] == 1


@pytest.mark.parametrize('status',['failed','completed'])
def test_unknown_result_stops_new_requests_without_releasing_reservation(journal,status):
    call=journal.reserve('book-a','llm','fixture',1000,0)
    journal.finish(call,status=status)
    with pytest.raises(BudgetExceeded,match='待确认'):
        journal.reserve('book-c','llm','fixture',1000,0)
    assert journal.validation_budget_summary('goal')['totals']['charged_cost'] == pytest.approx(.01)


def test_budget_cannot_be_reset_or_book_costs_transferred(journal):
    journal.register_validation_budget('goal',policy())
    changed=policy();changed['limit']=3
    with pytest.raises(ValueError,match='重置'):
        journal.register_validation_budget('goal',changed)
    with pytest.raises(ValueError,match='转移'):
        journal.enroll_validation_book('book-a','goal','longrun')
    with pytest.raises(ValueError):
        journal.enroll_validation_book('other','goal','missing-stage')


def test_unpriced_models_fail_closed_and_normal_works_are_unchanged(journal):
    with pytest.raises(BudgetExceeded,match='单价'):
        journal.reserve('book-a','embedding','missing-model',10,0)
    id=journal.reserve('ordinary-work','llm','unpriced-model',10,0)
    journal.finish(id,10,0)
    assert journal.validation_budget_summary('goal')['totals']['calls'] == 0


def test_strict_validation_rejects_unscoped_and_unregistered_calls(journal,monkeypatch):
    monkeypatch.setenv('XUHENG_VALIDATION_BUDGET_REQUIRED','1')
    with pytest.raises(BudgetExceeded,match='归属'):
        ModelCall('llm','fixture','prompt',10)
    with usage_scope('ordinary-work',journal):
        with pytest.raises(BudgetExceeded,match='绑定'):
            ModelCall('llm','fixture','prompt',10)
    with usage_scope('book-a',journal):
        call=ModelCall('llm','fixture','prompt',10)
        call.finish(10,5)
    assert journal.validation_budget_summary('goal')['totals']['calls'] == 1


def test_settled_usage_releases_only_unused_reservation(journal):
    first=journal.reserve('book-a','llm','fixture',90000,0)
    journal.finish(first,10000,0)
    second=journal.reserve('book-b','llm','fixture',90000,0)
    assert second
    assert journal.validation_budget_summary('goal')['totals']['charged_cost'] == pytest.approx(1.0)


def test_existing_calls_cannot_be_hidden_by_late_enrollment(journal):
    journal.reserve('ordinary-work','llm','fixture',1000,0)
    with pytest.raises(ValueError,match='首次'):
        journal.enroll_validation_book('ordinary-work','goal','calibration')


def test_explicit_price_refresh_preserves_original_cost_and_all_limits(journal):
    call=journal.reserve('book-a','llm','fixture',10000,0)
    journal.finish(call,10000,0)
    changed=policy()
    changed['prices'][0]['input_per_million']=20
    journal.register_validation_budget('goal',changed,refresh_prices=True)
    assert journal.validation_budget_summary('goal')['totals']['charged_cost'] == pytest.approx(.1)
    new=journal.reserve('book-b','llm','fixture',10000,0)
    journal.finish(new,10000,0)
    assert journal.validation_budget_summary('goal')['totals']['charged_cost'] == pytest.approx(.3)
    with journal.connection() as conn:
        assert conn.execute('SELECT count(*) FROM validation_price_events').fetchone()[0] == 1
    changed['limit']=3
    with pytest.raises(ValueError,match='重置'):
        journal.register_validation_budget('goal',changed,refresh_prices=True)


@pytest.mark.parametrize('value',[float('inf'),float('nan'),21,True,-1])
def test_invalid_limits_rejected(tmp_path,value):
    journal=OperationJournal(tmp_path/'operations.sqlite3')
    body=policy();body['limit']=value
    with pytest.raises(ValueError):
        journal.register_validation_budget('invalid',body)
