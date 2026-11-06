from copy import deepcopy
from scripts.validation.autopilot_longrun import evaluate


def test_validation_identity_precedes_spa_fallback():
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    from fastapi.testclient import TestClient
    from scripts.validation.server import register_identity
    app=FastAPI()
    @app.get('/{path:path}')
    def spa(path):
        return HTMLResponse('<html>workbench</html>')
    register_identity(app,'fixture-run-id')
    with TestClient(app) as client:
        response=client.get('/internal/validation-status')
        assert response.headers['content-type'].startswith('application/json')
        assert response.json()['run_id']=='fixture-run-id'
        assert response.json()['isolated']


def complete():
    return {'chapters':[{'number':n,'characters':1000,'content_hash':str(n)} for n in range(1,11)],
            'novel':{'autopilot_status':'stopped','current_auto_chapters':10},
            'chapter_nodes':[{'number':n,'parent_id':'first' if n<=5 else 'second','planning_source':'ai_act'} for n in range(1,11)],
            'acts':[{'id':'first','chapter_start':1,'chapter_end':5,'planning_source':'ai_macro'},
                    {'id':'second','chapter_start':6,'chapter_end':10,'planning_source':'ai_macro'}],
            'aftermath_tasks':[{'number':n,'content_hash':str(n),'status':'completed'} for n in range(1,11)],
            'budget':{'limit':20,'totals':{'unsettled':0,'unpriced':0,'charged_cost':1},'stages':{'longrun':{'calls':50,'charged_cost':1,'limit':12}}}}


def test_complete_longrun_still_does_not_imply_fault_matrix_is_passed():
    report=evaluate(complete(),True)
    assert report['longrun_passed']
    assert not report['full_f01_acceptance']
    assert report['missing_acceptance']


def test_runner_cleanup_stop_cannot_masquerade_as_automatic_target_stop():
    report=evaluate(complete())
    assert not report['longrun_passed']
    assert not report['requirements']['stopped_at_target']


def test_local_chapter_numbers_do_not_falsely_prove_cross_act():
    state=complete()
    state['acts'][1].update(chapter_start=11,chapter_end=15)
    state['chapter_nodes'][5]['number']=1
    report=evaluate(state,True)
    assert not report['requirements']['cross_act']


def test_old_body_aftermath_receipts_cannot_certify_current_chapter():
    state=complete()
    state['aftermath_tasks'][3]['content_hash']='old-body'
    assert not evaluate(state,True)['requirements']['aftermath_complete']


def test_manual_planning_or_trivial_chapters_fail_acceptance():
    state=complete()
    state['acts'][0]['planning_source']='manual'
    state['chapters'][0]['characters']=1
    report=evaluate(state,True)
    assert not report['requirements']['ai_macro_planning']
    assert not report['requirements']['chapter_lengths']


def test_unsettled_requests_or_exceeded_budget_fail_acceptance():
    state=complete()
    state['budget']['totals'].update(unsettled=1,charged_cost=21)
    report=evaluate(state,True)
    assert not report['requirements']['budget_known']
    assert not report['requirements']['within_total_budget']


def test_setup_resume_rejects_existing_paid_attempts_or_manuscript():
    from scripts.validation.autopilot_longrun import can_resume_setup

    state = complete()
    assert not can_resume_setup(state)
    for field in ('chapters', 'acts', 'chapter_nodes', 'aftermath_tasks'):
        state[field] = []
    assert not can_resume_setup(state)
    state['budget']['stages']['longrun']['calls'] = 0
    assert can_resume_setup(state)
