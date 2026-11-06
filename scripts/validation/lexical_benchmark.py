"""Offline F09 lexical benchmark with an isolated original synthetic corpus."""
import argparse
import asyncio
import json
import math
from pathlib import Path
import platform
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from application.audit.services.history_retrieval import HistoricalRetriever
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.evidence_review_store import EvidenceReviewStore
from infrastructure.persistence.database.search_index_store import SearchIndexStore


def fixture():
    names = ('紫铜 灰银 翠玉 琉璃 白石 琥珀 青金 乌木 星铁 素瓷 丹砂 碧蜡 玄冰 金砂 松脂 竹丝 朱墨 赤珊 瑶石 墨晶'
             ' 银毫 珠母 雪绒 桃木 橡皮 玄漆 铜丝 棉絮 藤条 黑曜').split()
    objects = ('灯芯 钥匙 密匣 桅杆 风铃 纸鸢 玉佩 火镰 磁针 瓷瓶 葫芦 令牌 手炉 沙漏 杯盏 画轴 瓦片 邮筒 望镜 绳结'
               ' 乐谱 贝壳 书签 鼓槌 算盘 棋子 墨盒 腰带 封泥 印章').split()
    queries = [name + item for name, item in zip(names, objects)]
    paragraphs = [
        '海风推着浅浪穿过堤坝，夜巡的船夫把绳索绕回旧木桩。北岸小镇陆续关门，只有码头西端的窗里还亮着烛光。',
        '雨落在茶摊的帆布上，掌柜数完今天收来的钱，把账本锁进抽屉。旅客赶往驿站，石板路上留下一串泥脚印。',
        '远处传来低沉的钟声。修理师收拾台面，给桌角的油壶加了一点清油，再用布擦干门口的工具。',
    ]
    chapters = {}
    for number in range(1, 302):
        preface = f'第{number}章。夜色平静，巡逻队换班。\n'
        if number <= len(queries):
            preface += f'林澈核对过仓库记录。登记的物证是{queries[number-1]}，仍放在编号为{number}的木柜。\n'
        if number == 301:
            preface += '未来记录：' + '。'.join(queries) + '。\n'
        chapters[number] = preface + '\n'.join(paragraphs[(number+i) % len(paragraphs)] for i in range(85))
    return chapters, queries


def run():
    chapters, queries = fixture()
    with TemporaryDirectory(prefix='xuheng-lexical-') as tmp:
        db = DatabaseConnection(str(Path(tmp)/'fixture.sqlite3'))
        index = SearchIndexStore(db)
        try:
            with index.connection(write=True) as conn:
                conn.executemany('INSERT INTO novels(id,title,slug) VALUES (?,?,?)', [('bench','潮汐试卷','bench'),('other','干扰作品','other')])
                conn.executemany('INSERT INTO chapters(id,novel_id,number,content) VALUES (?,?,?,?)',
                                 [(f'c{n}','bench',n,content) for n,content in chapters.items()])
                conn.execute('INSERT INTO chapters(id,novel_id,number,content) VALUES (?,?,?,?)', ('other1','other',1,'。'.join(queries)))
            begin = perf_counter()
            index.enqueue('bench')
            index.enqueue('other')
            for _ in range(len(chapters) + 5):
                if not index.process_next():
                    break
            index_seconds = perf_counter() - begin
            retriever = HistoricalRetriever(EvidenceReviewStore(db), index_store=index)

            async def measure():
                elapsed, misses = [], []
                citations = valid = leaks = 0
                await retriever.retrieve('bench', 301, queries[0])
                for chapter, query in enumerate(queries, 1):
                    started = perf_counter()
                    result = await retriever.retrieve('bench', 301, query)
                    elapsed.append(perf_counter() - started)
                    if not any(hit['chapter_id'] == f'c{chapter}' for hit in result['sources']):
                        misses.append(chapter)
                    for hit in result['sources']:
                        citations += 1
                        if hit['novel_id'] != 'bench' or hit['chapter_number'] >= 301:
                            leaks += 1
                        elif chapters[hit['chapter_number']][hit['start']:hit['end']] == hit['text']:
                            valid += 1
                return {'queries':len(queries), 'recall_at_6':1-len(misses)/len(queries), 'missed_chapters':misses,
                        'valid_citations':valid,'citations':citations,'leaks':leaks,
                        'warm_p95_seconds':sorted(elapsed)[math.ceil(.95*len(elapsed))-1]}

            report = asyncio.run(measure())
            report.update(chapters=300, characters=sum(len(chapters[n]) for n in range(1,301)), index_seconds=index_seconds,
                          machine={'platform':platform.platform(),'processor':platform.processor(),'python':platform.python_version()},
                          commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                          working_tree_dirty=bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT)),
                          scope='synthetic exact clues, lexical only, warm after one query, no paid calls')
            report['passed'] = (report['characters']>=1_000_000 and report['recall_at_6']>=.9 and
                                report['citations']>0 and report['valid_citations']==report['citations'] and
                                report['leaks']==0 and report['warm_p95_seconds']<=2)
            return report
        finally:
            db.close_all()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = run()
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(payload+'\n', encoding='utf-8')
    print(payload)
    raise SystemExit(0 if report['passed'] else 2)
