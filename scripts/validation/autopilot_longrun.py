"""F01 production-chain runner. Inspection is free; --send starts paid work.

Use the fixed workspace and immutable stage envelope; never pre-seed planning or
disable auxiliary calls to make this acceptance pass. This report does not claim
the separate fault-injection matrix has passed.
"""
from contextlib import closing
from datetime import datetime,timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from scripts.validation.goal_workspace import private_root,configure_environment,preflight,journal,GROUP

BOOK='goal-longrun'


def snapshot():
    target=private_root()/'data'/'plotpilot.db'
    with closing(sqlite3.connect(target.as_uri()+'?mode=ro',uri=True)) as conn:
        conn.row_factory=sqlite3.Row
        novel=conn.execute('SELECT autopilot_status,current_stage,current_act,current_auto_chapters,consecutive_error_count FROM novels WHERE id=?',(BOOK,)).fetchone()
        chapters=[dict(row) for row in conn.execute('SELECT id,number,length(content) AS characters,content FROM chapters WHERE novel_id=? ORDER BY number',(BOOK,))]
        for chapter in chapters:
            chapter['content_hash']=hashlib.sha256((chapter.pop('content') or '').encode()).hexdigest()
        tasks=[dict(row) for row in conn.execute('SELECT number,content_hash,status,phase,error FROM xh_aftermath_tasks WHERE novel_id=? ORDER BY number',(BOOK,))]
        acts=[dict(row) for row in conn.execute("SELECT id,number,chapter_start,chapter_end,planning_source FROM story_nodes WHERE novel_id=? AND node_type='act' ORDER BY number",(BOOK,))]
        nodes=[dict(row) for row in conn.execute("SELECT parent_id,number,planning_source FROM story_nodes WHERE novel_id=? AND node_type='chapter' ORDER BY number",(BOOK,))]
    return {'novel':dict(novel) if novel else None,'chapters':chapters,'aftermath_tasks':tasks,'acts':acts,'chapter_nodes':nodes,
            'budget':journal().validation_budget_summary(GROUP)}


def evaluate(state, automatic_stop_observed=False):
    chapters=state['chapters']
    covered=set()
    saved_numbers={c['number'] for c in chapters if c['characters']}
    # Never infer act coverage from chapter nodes numbered locally within acts.
    covered.update(act['id'] for act in state['acts'] if act['chapter_start'] is not None and act['chapter_end'] is not None
                   and any(act['chapter_start']<=number<=act['chapter_end'] for number in saved_numbers))
    current={c['number']:c['content_hash'] for c in chapters}
    tasks={task['number']:task for task in state['aftermath_tasks'] if current.get(task['number'])==task['content_hash']}
    budget=state['budget']
    requirements={
        'ten_saved_chapters':saved_numbers==set(range(1,11)),
        'chapter_lengths':len(chapters)==10 and all(c['characters']>=500 for c in chapters),
        'cross_act':len(covered)>=2,
        'ai_macro_planning':bool(state['acts']) and all(a['planning_source']=='ai_macro' for a in state['acts']),
        'ai_act_planning':bool(state['chapter_nodes']) and all(n['planning_source']=='ai_act' for n in state['chapter_nodes']),
        'aftermath_complete':all(n in tasks and tasks[n]['status']=='completed' for n in range(1,11)),
        'stopped_at_target':automatic_stop_observed and bool(state['novel']) and state['novel']['autopilot_status']=='stopped' and state['novel']['current_auto_chapters']==10,
        'budget_known':budget['totals']['unsettled']==0 and budget['totals']['unpriced']==0,
        'within_total_budget':budget['totals']['charged_cost']<=budget['limit'],
        'within_longrun_budget':budget['stages']['longrun']['charged_cost']<=budget['stages']['longrun']['limit'],
        'real_requests_recorded':budget['stages']['longrun']['calls']>0,
    }
    return {'longrun_passed':all(requirements.values()),'requirements':requirements,'covered_acts':len(covered),
            'full_f01_acceptance':False,'missing_acceptance':['独立的暂停、限流、断网、重启、章数与预算停止矩阵仍须留存证据']}


def request(port,path,method='GET',body=None):
    data=None if body is None else json.dumps(body,ensure_ascii=False).encode()
    req=urllib.request.Request('http://127.0.0.1:'+str(port)+path,data=data,method=method,
                               headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=25) as response:
        return json.load(response)


def can_resume_setup(state):
    return (state['novel'] is not None and state['novel']['autopilot_status'] == 'stopped'
            and not state['chapters'] and not state['acts'] and not state['chapter_nodes']
            and not state['aftermath_tasks'] and state['budget']['stages']['longrun']['calls'] == 0)


def ensure_story_inputs(port):
    path = '/api/v1/bible/novels/' + BOOK + '/bible'
    bible = request(port, path)
    existing = {character['id'] for character in bible['characters']}
    for character in (
        {'character_id': BOOK+'-lin', 'name': '林澈', 'description': '灯塔修复师，擅长修理灯具，谨慎记录每次点灯。', 'role': '主角'},
        {'character_id': BOOK+'-su', 'name': '苏禾', 'description': '守潮人，熟悉港口潮汐，保管失踪船只的旧航海日志。', 'role': '搭档'},
    ):
        if character['character_id'] not in existing:
            request(port, path+'/characters', 'POST', character)


def run(port,seconds, resume_setup=False):
    ready=preflight('longrun')
    if not ready['ready']:
        return {'started':False,**ready}
    before=snapshot()
    if before['novel'] is not None and not (resume_setup and can_resume_setup(before)):
        return {'started':False,'reason':'已有长跑作品，保留现场；先检查已有结果，不自动重启或再生成','state':before,'acceptance':evaluate(before)}
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1',port))==0:
            raise ValueError('隔离端口已占用，不连接或停止未知进程')
    from infrastructure.persistence.backup_service import file_lock
    with closing(file_lock(private_root()/'longrun.lock')):
        if snapshot() != before:
            raise RuntimeError('验收数据在预检查后变化，请重新检查账本')
        run_id=uuid4().hex
        log=(private_root()/'longrun-server.log').open('ab')
        process=subprocess.Popen([sys.executable,'scripts/validation/server.py','--port',str(port),'--run-id',run_id],
                                 cwd=ROOT,env=os.environ.copy(),stdout=log,stderr=log,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        identity=False
        events=private_root()/'longrun-events.jsonl'
        automatic_stop=False
        try:
            deadline=time.monotonic()+60
            while time.monotonic()<deadline:
                if process.poll() is not None:
                    raise RuntimeError('隔离服务在就绪前退出')
                try:
                    status=request(port,'/internal/validation-status')
                    # Windows venv launchers can proxy the interpreter process;
                    # the unguessable per-run token identifies the owned API.
                    identity=status['run_id']==run_id and status.get('isolated') is True
                    if identity:
                        break
                except (OSError,ValueError):
                    pass
                time.sleep(.25)
            if not identity:
                raise RuntimeError('隔离服务身份校验失败')
            if before['novel'] is None:
                request(port,'/api/v1/novels/','POST',{
                    'novel_id':BOOK,'title':'隔离验收 · 潮汐灯塔','author':'叙衡验收示例','target_chapters':10,
                    'target_words_per_chapter':1000,'premise':'灯塔修复师林澈与守潮人苏禾调查失踪的航船。旧灯芯只能燃烧三次，第三次将揭示港口失踪案。全书十章，严格分为两幕，每幕五章；前五章调查，第六章进入解谜和抉择，十章结束。',
                    'genre':'悬疑','special_requirements':'必须两幕，每幕五章。先生成完整规划，再逐章写作。人物能力、物品次数及事件时间保持可追溯；每章约1000字。'})
            ensure_story_inputs(port)
            request(port,'/api/v1/novels/'+BOOK+'/auto-approve-mode','PATCH',{'auto_approve_mode':True})
            request(port,'/api/v1/autopilot/'+BOOK+'/start','POST',{'max_auto_chapters':10,'target_chapters':10,'target_words_per_chapter':1000})
            deadline=time.monotonic()+seconds
            previous=None
            while time.monotonic()<deadline:
                if process.poll() is not None:
                    break
                state=snapshot()
                fingerprint=json.dumps(state,sort_keys=True,ensure_ascii=False)
                if fingerprint!=previous:
                    with events.open('a',encoding='utf-8') as stream:
                        stream.write(json.dumps({'at':datetime.now(timezone.utc).isoformat(),'state':state},ensure_ascii=False)+'\n')
                    previous=fingerprint
                if state['novel'] and state['novel']['autopilot_status']=='stopped':
                    automatic_stop=state['novel']['current_auto_chapters']==10 and len(state['chapters'])==10
                    if all(task['status'] not in ('queued','running') for task in state['aftermath_tasks']):
                        break
                if state['budget']['totals']['unknown_finished']:
                    break
                time.sleep(2)
        finally:
            if identity and process.poll() is None:
                try:
                    request(port,'/api/v1/autopilot/'+BOOK+'/stop','POST',{})
                    request(port,'/internal/shutdown','POST',{})
                except (OSError,ValueError):
                    pass
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                # Only the owned API; its normal shutdown owns the daemon. Never scan/kill unrelated processes.
                process.terminate()
                process.wait(timeout=10)
            log.close()
        state=snapshot()
        result={'started':True,'state':state,'automatic_stop_observed':automatic_stop,'acceptance':evaluate(state,automatic_stop)}
        (private_root()/'longrun-report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--send',action='store_true')
    parser.add_argument('--resume-setup',action='store_true',help='仅续跑尚无模型请求和规划结果的隔离作品')
    parser.add_argument('--port',type=int,default=8013)
    parser.add_argument('--seconds',type=int,default=1800)
    args=parser.parse_args()
    configure_environment()
    try:
        if args.send:
            result=run(args.port,args.seconds,args.resume_setup)
        else:
            state=snapshot()
            result={'state':state,'acceptance':evaluate(state),'preflight':preflight('longrun')}
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0 if result.get('acceptance',{}).get('longrun_passed') else 2
    except Exception as exc:
        print(json.dumps({'passed':False,'error_type':type(exc).__name__,'message':'验收未通过，保留隔离现场与账本，不自动重试'},ensure_ascii=False))
        return 2


if __name__=='__main__':
    raise SystemExit(main())
