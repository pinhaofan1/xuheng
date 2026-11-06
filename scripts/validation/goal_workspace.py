"""Private, persistent validation workspace. Default commands never call a model.

prepare copies model configuration only, never manuscripts, into .validation.
calibrate requires --send and a recent price attestation; calls use the real
provider and the same durable operation ledger as later validation books.
"""
from contextlib import closing
from datetime import datetime, timezone
import argparse
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
WORKSPACE=ROOT/'.validation'/'goal-remaining-20261105'
GROUP='xuheng-remaining-20261105'
TABLES=('llm_profiles','llm_config_meta','embedding_config','xh_retrieval_settings')


def private_root():
    path=WORKSPACE.resolve()
    if not path.is_relative_to((ROOT/'.validation').resolve()):
        raise ValueError('验收路径不在隔离目录中')
    path.mkdir(parents=True,exist_ok=True)
    return path


def configure_environment():
    root=private_root()
    os.environ.update(PLOTPILOT_PROD_DATA_DIR=str(root/'data'),DISABLE_AUTO_DAEMON='1',
                      XUHENG_VALIDATION_BUDGET_REQUIRED='1',EMBEDDING_SERVICE='openai',
                      LOG_FILE=str(root/'application.log'),PYTHONIOENCODING='utf-8')
    # The copied local profiles are the only credential source for this run.
    for name in ('OPENAI_API_KEY','ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN','ARK_API_KEY','GEMINI_API_KEY'):
        os.environ.pop(name,None)
    return root/'data'/'plotpilot.db'


def journal():
    from infrastructure.persistence.operation_journal import OperationJournal
    return OperationJournal(private_root()/'data'/'plotpilot.operations.sqlite3')


def copy_configuration(source, target):
    """Insert only the known config tables, with explicit columns and no output."""
    if Path(source).resolve() == Path(target).resolve():
        raise ValueError('源数据与验收数据不能相同')
    with closing(sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro',uri=True)) as src:
        available={row[0] for row in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        with closing(sqlite3.connect(target)) as dest, dest:
            for table in TABLES:
                if table not in available:
                    continue
                columns=[row[1] for row in src.execute('PRAGMA table_info('+table+')')]
                target_columns={row[1] for row in dest.execute('PRAGMA table_info('+table+')')}
                columns=[column for column in columns if column in target_columns]
                names=','.join('"'+column+'"' for column in columns)
                rows=src.execute('SELECT '+names+' FROM '+table).fetchall()
                dest.executemany('INSERT INTO '+table+' ('+names+') VALUES ('+','.join('?' for _ in columns)+')',rows)


def prepare(prices, source=None):
    # Resolve the real source before binding application.paths to the workspace.
    if source is None:
        from application.paths import get_db_path
        source=get_db_path()
    target=configure_environment()
    root=private_root()
    marker=root/'prepared.json'
    if target.exists() and not marker.exists():
        raise ValueError('存在未确认的验收数据；保留现场并检查，不自动覆盖或重置')
    from infrastructure.persistence.database.connection import DatabaseConnection
    db=DatabaseConnection(str(target))
    try:
        if not marker.exists():
            copy_configuration(source,target)
            (root/'prices.json').write_text(json.dumps(prices,ensure_ascii=False,indent=2),encoding='utf-8')
            marker.write_text(json.dumps({'format':1,'group':GROUP,'created_at':datetime.now(timezone.utc).isoformat()}),encoding='utf-8')
        else:
            if json.loads((root/'prices.json').read_text(encoding='utf-8')) != prices:
                raise ValueError('价格或预算与既有验收不同，不重置累计账本')
        ledger=journal()
        ledger.register_validation_budget(GROUP,prices)
        for stage in prices['stages']:
            ledger.enroll_validation_book('goal-'+stage,GROUP,stage)
        return {'prepared':True,'workspace':str(root),'budget':ledger.validation_budget_summary(GROUP)}
    finally:
        db.close_all()


def preflight(stage):
    target=configure_environment()
    prices=json.loads((private_root()/'prices.json').read_text(encoding='utf-8'))
    ledger=journal()
    summary=ledger.validation_budget_summary(GROUP)
    reasons=[]
    if ledger.validation_budget_config(GROUP) != prices:
        reasons.append('价格记录与账本不一致；先完成价格刷新，不发起模型请求')
    if summary['totals']['unsettled']:
        reasons.append('账本存在未结算或未知请求；先核对，不自动重发')
    with closing(sqlite3.connect(target.as_uri()+'?mode=ro',uri=True)) as conn:
        active=conn.execute("SELECT value FROM llm_config_meta WHERE key='active_profile_id'").fetchone()
        profile=conn.execute('SELECT model,base_url,protocol,length(api_key) FROM llm_profiles WHERE id=?',(active[0] if active else '',)).fetchone()
        if not profile or not profile[3]:
            reasons.append('本地聊天档案缺少有效配置或密钥')
            configured=[]
        else:
            from urllib.parse import urlsplit
            if urlsplit(profile[1]).hostname != 'api.deepseek.com' or profile[2] != 'openai':
                reasons.append('当前价格依据仅覆盖 DeepSeek 官方兼容接口')
            configured=[('llm',profile[0])]
        if stage != 'calibration':
            embedding=conn.execute('SELECT mode,model,length(api_key) FROM embedding_config LIMIT 1').fetchone()
            if embedding and embedding[0]=='openai' and embedding[2]:
                configured.append(('embedding',embedding[1]))
            elif not embedding:
                reasons.append('完整验收缺少检索配置')
            retrieval=conn.execute('SELECT reranker_model FROM xh_retrieval_settings LIMIT 1').fetchone()
            if retrieval and retrieval[0]:
                configured.append(('rerank',retrieval[0]))
        for kind,model in configured:
            price=next((p for p in prices['prices'] if p['kind']==kind and p['model']==model),None)
            if price is None:
                reasons.append('缺少已核对单价：'+kind+'/'+model)
                continue
            checked=datetime.fromisoformat(price['checked_at'])
            age=(datetime.now(timezone.utc)-checked.astimezone(timezone.utc)).total_seconds()
            if checked.tzinfo is None or not 0 <= age <= 86400:
                reasons.append('模型价格核对已过期或日期无效：'+model)
    return {'ready':not reasons,'stage':stage,'reasons':reasons,'budget':summary}


async def calibrate():
    from infrastructure.persistence.backup_service import file_lock
    with closing(file_lock(private_root()/'calibration.lock')):
        return await _calibrate_locked()


async def _calibrate_locked():
    result=preflight('calibration')
    if not result['ready']:
        return result
    from application.ai.usage_accounting import usage_scope
    from infrastructure.ai.provider_factory import DynamicLLMService
    from domain.ai.services.llm_service import GenerationConfig
    from domain.ai.value_objects.prompt import Prompt
    # Never repeat calibration merely because the command was restarted.
    ledger=journal()
    if ledger.validation_budget_summary(GROUP)['stages']['calibration']['calls']:
        return {'sent':False,'reason':'校准已有请求记录，不自动重复','budget':ledger.validation_budget_summary(GROUP)}
    with usage_scope('goal-calibration',ledger,'持续目标：真实接口校准'):
        result=await DynamicLLMService().generate(Prompt(system='请只回答接口校准通过。',user='这是叙衡隔离验收的连通性检查。'),GenerationConfig(max_tokens=64))
    return {'sent':True,'response_nonempty':bool(result.content.strip()),'budget':ledger.validation_budget_summary(GROUP)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','status','preflight','calibrate','refresh-prices'])
    parser.add_argument('--prices',type=Path,default=ROOT/'scripts/validation/prices-2026-11-05.json')
    parser.add_argument('--stage',choices=['calibration','longrun','retrieval','regression'],default='longrun')
    parser.add_argument('--send',action='store_true')
    args=parser.parse_args()
    try:
        if args.action=='prepare':
            result=prepare(json.loads(args.prices.read_text(encoding='utf-8')))
        elif args.action=='refresh-prices':
            configure_environment()
            prices=json.loads(args.prices.read_text(encoding='utf-8'))
            journal().register_validation_budget(GROUP,prices,refresh_prices=True)
            from infrastructure.persistence.backup_service import write_json
            write_json(private_root()/'prices.json',prices)
            result={'prices_refreshed':True,'budget':journal().validation_budget_summary(GROUP)}
        elif args.action=='status':
            result=journal().validation_budget_summary(GROUP)
        elif args.action=='calibrate' and args.send:
            result=asyncio.run(calibrate())
        else:
            result=preflight('calibration' if args.action=='calibrate' else args.stage)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 2 if result.get('ready') is False else 0
    except Exception as exc:
        # Provider and config exceptions can contain credentials; never echo them.
        print(json.dumps({'success':False,'error_type':type(exc).__name__,
                          'message':'验收未完成，已保留隔离账本；检查脱敏日志与状态，不自动重试'},ensure_ascii=False))
        return 2


if __name__=='__main__':
    raise SystemExit(main())
