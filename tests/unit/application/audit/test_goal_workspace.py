from contextlib import closing
from datetime import datetime, timezone, timedelta
import json
import os
import sqlite3

import pytest

from scripts.validation import goal_workspace as workspace
from infrastructure.persistence.database.connection import DatabaseConnection


@pytest.fixture
def isolated(tmp_path,monkeypatch):
    monkeypatch.setattr(workspace,'ROOT',tmp_path)
    monkeypatch.setattr(workspace,'WORKSPACE',tmp_path/'.validation'/'goal-remaining-20261105')
    for name in ('PLOTPILOT_PROD_DATA_DIR','DISABLE_AUTO_DAEMON','XUHENG_VALIDATION_BUDGET_REQUIRED',
                 'EMBEDDING_SERVICE','LOG_FILE','PYTHONIOENCODING','OPENAI_API_KEY','ANTHROPIC_API_KEY',
                 'ANTHROPIC_AUTH_TOKEN','ARK_API_KEY','GEMINI_API_KEY'):
        monkeypatch.setenv(name,os.environ.get(name,''))
    source=tmp_path/'source'/'plotpilot.db'
    db=DatabaseConnection(str(source))
    try:
        with closing(sqlite3.connect(source)) as conn,conn:
            conn.execute("INSERT INTO novels(id,title,slug) VALUES ('private-work','private text','private-work')")
            conn.execute("INSERT INTO llm_profiles(id,name,model,base_url,api_key) VALUES ('profile','fixture','deepseek-flash','https://api.deepseek.com','test-credential-do-not-export')")
            conn.execute("INSERT INTO llm_config_meta VALUES ('active_profile_id','profile')")
            conn.execute("INSERT INTO embedding_config(id,mode,model,api_key) VALUES ('default','openai','Qwen/Qwen3-Embedding-4B','test-embedding-credential')")
            conn.execute("INSERT INTO xh_retrieval_settings VALUES ('default','Qwen/Qwen3-Reranker-4B',1024,'fixture')")
    finally:
        db.close_all()
    prices={'currency':'CNY','limit':20,'stages':{'calibration':2,'longrun':12,'retrieval':3,'regression':3},
            'prices':[{'kind':'llm','model':'deepseek-flash','input_per_million':2,'output_per_million':8,
                       'source':'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/',
                       'checked_at':datetime.now(timezone.utc).isoformat()}]}
    return source,prices


def test_prepare_copies_only_configuration_and_preserves_user_data(isolated):
    source,prices=isolated
    first=workspace.prepare(prices,source)
    assert first['prepared']
    target=workspace.private_root()/'data'/'plotpilot.db'
    with closing(sqlite3.connect(target)) as conn:
        assert conn.execute('SELECT count(*) FROM novels').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM chapters').fetchone()[0] == 0
        assert conn.execute('SELECT api_key FROM llm_profiles').fetchone()[0] == 'test-credential-do-not-export'
    with closing(sqlite3.connect(source)) as conn:
        assert conn.execute('SELECT count(*) FROM novels').fetchone()[0] == 1
    second=workspace.prepare(prices,source)
    assert second['budget'] == first['budget']
    assert 'test-credential' not in json.dumps(first)


def test_preflight_does_not_invent_missing_embedding_prices(isolated):
    source,prices=isolated
    workspace.prepare(prices,source)
    assert workspace.preflight('calibration')['ready']
    full=workspace.preflight('longrun')
    assert not full['ready']
    assert any('embedding/Qwen/Qwen3-Embedding-4B' in reason for reason in full['reasons'])
    assert any('rerank/Qwen/Qwen3-Reranker-4B' in reason for reason in full['reasons'])
    assert workspace.journal().validation_budget_summary(workspace.GROUP)['totals']['calls'] == 0


def test_restart_cannot_reset_prices_or_budget(isolated):
    source,prices=isolated
    workspace.prepare(prices,source)
    changed={**prices,'stages':{'calibration':10,'longrun':10}}
    with pytest.raises(ValueError,match='不重置'):
        workspace.prepare(changed,source)


def test_pending_request_blocks_resuming_paid_validation(isolated):
    source,prices=isolated
    workspace.prepare(prices,source)
    workspace.journal().reserve('goal-calibration','llm','deepseek-flash',1000,64)
    result=workspace.preflight('calibration')
    assert not result['ready']
    assert result['budget']['totals']['unsettled'] == 1


def test_expired_price_attestation_blocks_paid_validation(isolated):
    source,prices=isolated
    prices['prices'][0]['checked_at']=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
    workspace.prepare(prices,source)
    assert not workspace.preflight('calibration')['ready']


def test_private_root_cannot_escape_validation_directory(isolated,tmp_path,monkeypatch):
    monkeypatch.setattr(workspace,'WORKSPACE',tmp_path/'outside')
    with pytest.raises(ValueError,match='隔离目录'):
        workspace.private_root()
