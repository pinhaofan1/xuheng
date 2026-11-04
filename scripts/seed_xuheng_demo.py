r"""Create an additive local review example; never overwrite existing content.

Run from the repository root: .venv\Scripts\python.exe scripts/seed_xuheng_demo.py
"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.audit.evidence_models import CharacterRule, RuleSet
from application.audit.services.evidence_review_service import EvidenceReviewService
from infrastructure.persistence.database.connection import get_database
from infrastructure.persistence.database.evidence_review_store import EvidenceReviewStore
from infrastructure.persistence.database.write_dispatch import sqlite_writes_bypass_queue


def main():
    db = get_database()
    try:
        # Startup/standalone initialization only; no await or running API tasks.
        with sqlite_writes_bypass_queue():
            with db.transaction() as tx:
                tx.execute("""INSERT OR IGNORE INTO novels(id, slug, title, author, target_chapters, premise)
                    VALUES ('xuheng-demo', 'xuheng-demo', '潮汐与余烬 · 审校示例', '叙衡示例', 2,
                    '林烬仅会火系，沈澜仅会水系。用明确设定与原文证据核对行为。')""")
                for number, title, content in [
                    (1, "渡口", "暮色落在渡口。\n\n林烬释放火球，照亮了岸边的石阶。沈澜施展水系法术，将小船推离浅滩。\n\n两人隔着水面相望，谁也没有说话。"),
                    (2, "潮声里的枪响", "🌊潮水漫过石阶，林烬站在岸边。\n\n林烬释放火球，沈澜施展水系法术。\n\n沈澜释放火球，照亮了远处的船帆。\n\n沈澜开枪，枪声惊起了芦苇里的水鸟。\n\n如果林烬施展水系法术，也许能让船停下来。\n\n沈澜没有释放火球，他只是看向了水面。"),
                ]:
                    tx.execute("""INSERT OR IGNORE INTO chapters(id, novel_id, number, title, content, outline, status)
                        VALUES (?, 'xuheng-demo', ?, ?, ?, '', 'draft')""",
                        (f"xuheng-demo-ch{number}", number, title, content))
            service = EvidenceReviewService(EvidenceReviewStore(db))
            if not service.get_rules("xuheng-demo").revision:
                service.save_rules("xuheng-demo", RuleSet(profiles=[
                    CharacterRule(id="lin-jin", name="林烬", aliases=["阿烬"], allowed_magic=["火系"]),
                    CharacterRule(id="shen-lan", name="沈澜", aliases=["阿澜"], allowed_magic=["水系"], forbidden_actions=["use_gun"]),
                ]))
            result = service.review("xuheng-demo", 2)
        print(f"Example ready: {len(result['issues'])} conflicts; {len(result['uncertain'])} uncertain.")
        print("Open http://127.0.0.1:8005/book/xuheng-demo/review/2")
    finally:
        db.close_all()


if __name__ == "__main__":
    main()
