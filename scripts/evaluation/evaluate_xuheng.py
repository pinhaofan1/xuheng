"""Reproducible offline comparison with the pinned upstream rule service.

No LLM, network, database or secrets are needed. This measures a narrow rule
detector on synthetic regression examples, not the upstream AI review pipeline.
"""
from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import statistics
import subprocess
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from application.audit.evidence_models import ENGINE_VERSION, text_hash
from application.audit.services.evidence_review_service import review_text
from domain.novel.services.action_attribution import ACTION_LABELS
from scripts.evaluation.xuheng_cases import DEV_RULES, TEST_RULES, cases

BASELINE = "8dba88fad06368a632522ce6f98e17f9cfc7db2a"
BASELINE_SHA256 = "e46ae870eeb68e3b8a9b94aae8537f96a6c258544523b09c88e2cd5e9f04f93d"


def score(tp, fp, fn):
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall,
            "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="project_docs/evaluation.json")
    args = parser.parse_args()
    # The source archive has no .git. Both paths must use the exact pinned bytes.
    try:
        source_bytes = subprocess.run(
            ["git", "show", f"{BASELINE}:application/audit/services/conflict_detection_service.py"],
            cwd=ROOT, check=True, capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        source_bytes = (Path(__file__).parent / "baseline_conflict_service.txt").read_bytes()
    if hashlib.sha256(source_bytes).hexdigest() != BASELINE_SHA256:
        raise RuntimeError("Pinned baseline checksum mismatch; refusing an invalid comparison")
    source = source_bytes.decode("utf-8")
    namespace = {"__name__": "xuheng_pinned_upstream"}
    exec(compile(source, "pinned_upstream_conflict_service.py", "exec"), namespace)
    legacy = namespace["ConflictDetectionService"]()
    logging.getLogger("xuheng_pinned_upstream").setLevel(logging.WARNING)
    items = cases()
    counts = {key: [0, 0, 0] for key in ("baseline", "xuheng")}
    times = {key: [] for key in counts}
    details = []
    valid_quotes = total_quotes = uncertain_cases = 0
    for case in items:
        rules = DEV_RULES if case["split"] == "development" else TEST_RULES
        names = {p.name: p.id for p in rules.profiles}
        names.update({alias: p.id for p in rules.profiles for alias in p.aliases})
        states = {p.id: {"magic_type": "、".join(p.allowed_magic)} for p in rules.profiles}
        started = perf_counter()
        old = legacy.detect(case["text"], states, names)
        old_ms = (perf_counter() - started) * 1000
        started = perf_counter()
        issues, uncertain = review_text(case["text"], rules)
        new_ms = (perf_counter() - started) * 1000
        expected = set(map(tuple, case["expected"]))
        # The legacy response uses Chinese magic labels; map to the same codes.
        codes = {label: code for code, label in ACTION_LABELS.items()}
        predictions = {
            "baseline": {(x.entity_id, codes[x.actual]) for x in old if x.actual in codes},
            "xuheng": {(x["entity_id"], x["action"]) for x in issues},
        }
        if case["split"] == "evaluation":
            for key, predicted in predictions.items():
                for pos, count in enumerate((len(expected & predicted), len(predicted - expected), len(expected - predicted))):
                    counts[key][pos] += count
            times["baseline"].append(old_ms)
            times["xuheng"].append(new_ms)
            uncertain_cases += bool(uncertain)
            for issue in issues:
                total_quotes += 1
                valid_quotes += (case["text"][issue["start"]:issue["end"]] == issue["quote"] and text_hash(issue["source_quote"]) == issue["source_hash"])
        details.append({**case, "baseline": sorted(predictions["baseline"]), "xuheng": sorted(predictions["xuheng"]),
                        "uncertain_count": len(uncertain), "passed": predictions["xuheng"] == expected})
    report = {
        "baseline_commit": BASELINE, "engine_version": ENGINE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset_hash": text_hash(json.dumps(items, ensure_ascii=False, sort_keys=True)),
        "dataset": {"total": len(items), "development": 20, "evaluation": 60,
                    "provenance": "agent-authored synthetic regression cases, not independently human-reviewed or blind data"},
        "scope": "explicit actor/action conclusions from the pinned rule detector; not a comparison with the full AI review service",
        "metrics": {key: score(*value) for key, value in counts.items()},
        "timing": {key: {"n": len(value), "median_ms": statistics.median(value), "p95_ms": sorted(value)[math.ceil(len(value) * .95) - 1]} for key, value in times.items()},
        "evidence": {"valid": valid_quotes, "total": total_quotes},
        "uncertain_cases": uncertain_cases, "coverage": (60 - uncertain_cases) / 60,
        "model_calls": 0, "details": details,
    }
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# 叙衡离线规则评测", "", f"基线：`{BASELINE}`；规则版本：`{ENGINE_VERSION}`。", "",
             "80 条自编合成样例（20 条开发、60 条固定评测），未经独立人工复核，不是盲测集。",
             "比较范围仅为明确能力约束下的人物 / 行为结论，不能据此声称提升了完整 AI 审稿服务或真实用户作品的效果。", "",
             "| 检测器 | TP | FP | FN | 精确率 | 召回率 | F1 |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for name, m in report["metrics"].items():
        lines.append(f"| {name} | {m['tp']} | {m['fp']} | {m['fn']} | {m['precision']:.1%} | {m['recall']:.1%} | {m['f1']:.1%} |")
    lines += ["", f"证据引用有效：{valid_quotes}/{total_quotes}；含待确认项的样例：{uncertain_cases}/60；无待确认项覆盖率：{report['coverage']:.1%}。",
              "没有调用模型，Token 和模型费用不适用。延迟是本地单轮规则执行时间，样本较小、未作生产负载测试，详见 JSON。", "",
              "## 未通过的固定评测样例", ""]
    for case in details:
        if case["split"] == "evaluation" and not case["passed"]:
            lines.append(f"- `{case['id']}`：{case['text']} 预期 `{case['expected']}`，得到 `{case['xuheng']}`。")
    lines += ["", "已知边界：词表外动作存在漏报；间接回忆、外部道具和幻术解释仍可能误报。保留这些挑战案例，不通过改标签制造指标提升。", "",
              "实现与数据由同一开发过程产生，指标可能乐观。真实求职表述应同时披露样本规模、合成来源、评测范围及失败案例。"]
    output.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"metrics": report["metrics"], "evidence": report["evidence"], "report": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
