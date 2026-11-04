"""Explicit real-service smoke test on an additive fictional demo only.

Uses locally activated credentials without printing them. One retrieval and one
bounded chapter request; writes reports to ignored .validation, never to source.
"""
import argparse
import json
from pathlib import Path
import time
import urllib.request
import urllib.error

ROOT=Path(__file__).resolve().parents[1]
BASE='http://127.0.0.1:8005/api/v1'
BOOK='xuheng-integration-demo'


def call(path, data=None, method=None):
    request=urllib.request.Request(BASE+path, data=json.dumps(data,ensure_ascii=False).encode() if data is not None else None,
        headers={'Content-Type':'application/json'},method=method)
    return json.load(urllib.request.urlopen(request,timeout=180))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--generation',action='store_true',help='Also exercise one real chapter generation request')
    args=parser.parse_args()
    try: call('/novels/'+BOOK)
    except urllib.error.HTTPError as error:
        if error.code!=404: raise
        call('/novels/',{'novel_id':BOOK,'title':'雾港来信 · 联调示例','author':'虚构测试数据','target_chapters':3,
                        'target_words_per_chapter':500,'premise':'修船工沈澜带着旧信前往雾港，寻找失踪的同伴。'})
    chapters=call('/novels/'+BOOK+'/chapters')
    numbers={chapter['number'] for chapter in chapters}
    for number,title,text in [(1,'断桨','沈澜在渡口被坠落的木梁砸伤，左肢骨折，不能举起。医生给他裹上夹板，嘱咐七天内不能拆除。\n\n他把铜钥匙交给了同行的顾舟。'),
        (2,'旧信','第二天清晨，沈澜拆下夹板，毫无疼痛地抬起左臂。他说铜钥匙仍在自己口袋里，从来没有交给别人。'),
        (3,'雾港','待写大纲：沈澜带着夹板抵达雾港，与顾舟核对钥匙和旧信。')]:
        if number not in numbers:
            call('/novels/'+BOOK+'/chapters',{'chapter_id':BOOK+'-'+str(number),'number':number,'title':title,'content':text})
    rules=call('/evidence-reviews/'+BOOK+'/rules')
    if not rules['revision']:
        call('/evidence-reviews/'+BOOK+'/rules',{'revision':'','profiles':[{'id':'shen','name':'沈澜','aliases':[],
            'facts':['在渡口左臂骨折，医生嘱咐七天内不能拆夹板。'],'allowed_magic':[],'forbidden_actions':[]}]},'PUT')
    started=time.perf_counter()
    retrieved=call('/evidence-reviews/'+BOOK+'/chapters/2/context',{'query':'手臂是否健康，钥匙归谁保管','retrieval_mode':'hybrid_rerank'})
    report={'fictional_demo':BOOK,'retrieval':{'mode':retrieved['mode'],'sources':len(retrieved['sources']),
        'chapters':sorted({s['chapter_number'] for s in retrieved['sources']}),'usage':retrieved.get('retrieval_usage'),
        'elapsed_ms':round((time.perf_counter()-started)*1000,2)}}
    assert all(s['chapter_number']<2 for s in retrieved['sources'])
    if args.generation:
        request=urllib.request.Request(BASE+'/novels/'+BOOK+'/hosted-write-stream',data=json.dumps({
            'from_chapter':3,'to_chapter':3,'auto_save':False,'auto_outline':False}).encode(),headers={'Content-Type':'application/json'},method='POST')
        events=[]
        with urllib.request.urlopen(request,timeout=180) as response:
            for line in response:
                if line.startswith(b'data: '):
                    event=json.loads(line[6:]);events.append(event)
                    if event.get('type')=='error': break
        done=[event for event in events if event.get('type')=='done']
        report['generation']={'done':bool(done),'characters':len(done[-1].get('content','')) if done else 0,
            'event_types':sorted({event.get('type','') for event in events}),
            'errors':[event.get('message','') for event in events if event.get('type')=='error']}
        (ROOT/'.validation/generation-events.json').write_text(json.dumps(events,ensure_ascii=False,indent=2),encoding='utf-8')
    (ROOT/'.validation/integration-smoke.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True))


if __name__=='__main__': main()
