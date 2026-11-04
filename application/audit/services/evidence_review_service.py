"""Evidence-backed rule review; no network calls or fabricated model results."""
from __future__ import annotations

from datetime import datetime, timezone
from time import perf_counter
from typing import Protocol
from uuid import uuid4

from application.audit.evidence_models import AI_ENGINE_VERSION, ENGINE_VERSION, RuleSet, text_hash
from domain.novel.services.action_attribution import ACTION_LABELS, attribute_actions


class ReviewNotFound(ValueError):
    pass


class ReviewConflict(ValueError):
    pass


class ReviewUnavailable(RuntimeError):
    pass


class ReviewStore(Protocol):
    def require_novel(self, novel_id: str) -> None: ...
    def get_rules(self, novel_id: str) -> RuleSet: ...
    def save_rules(self, novel_id: str, rules: RuleSet) -> RuleSet: ...
    def get_chapter(self, novel_id: str, number: int | None = None, chapter_id: str | None = None) -> dict: ...
    def find_run(self, novel_id: str, chapter_id: str, content_hash: str, rules_hash: str) -> dict | None: ...
    def save_run(self, run: dict) -> dict: ...
    def get_run(self, novel_id: str, run_id: str) -> dict: ...
    def latest_run(self, novel_id: str, chapter_id: str) -> dict | None: ...
    def feedback(self, novel_id: str, run_id: str, issue_id: str, status: str, version: int) -> None: ...


def review_text(content: str, rules: RuleSet) -> tuple[list[dict], list[dict]]:
    """Pure rule evaluation, shared by the API and offline evaluation harness."""
    profiles = {p.id: p for p in rules.profiles}
    names = {p.name: p.id for p in rules.profiles}
    names.update({alias: p.id for p in rules.profiles for alias in p.aliases})
    issues, uncertain = [], []
    for mention in attribute_actions(content, names):
        profile = profiles.get(mention.entity_id)
        source = profile.source_text() if profile else ""
        candidate = {
            "id": text_hash(f"{mention.entity_id}:{mention.start}:{mention.end}:{mention.action}")[:24],
            "entity_id": mention.entity_id, "entity_name": mention.entity_name,
            "action": mention.action, "start": mention.start, "end": mention.end,
            "quote": mention.quote, "source_id": mention.entity_id,
            "source_quote": source, "source_hash": text_hash(source),
            "status": "pending", "feedback_version": 0,
        }
        if mention.reason:
            uncertain.append({**candidate, "kind": "uncertain", "description": mention.reason})
            continue
        if not profile:
            continue
        label = ACTION_LABELS[mention.action]
        forbidden = mention.action in profile.forbidden_actions
        wrong_magic = mention.action.endswith("_magic") and profile.allowed_magic and label not in profile.allowed_magic
        if forbidden or wrong_magic:
            issues.append({
                **candidate, "kind": "conflict",
                "description": f"{profile.name}出现“{label}”行为，与当前明确设定不符，请核对是否为剧情中的例外或设定变化。",
            })
    return issues, uncertain


class EvidenceReviewService:
    def __init__(self, store: ReviewStore):
        self.store = store

    def get_rules(self, novel_id: str) -> RuleSet:
        self.store.require_novel(novel_id)
        return self.store.get_rules(novel_id)

    def save_rules(self, novel_id: str, rules: RuleSet) -> RuleSet:
        self.store.require_novel(novel_id)
        return self.store.save_rules(novel_id, rules)

    def review(self, novel_id: str, number: int, expected_content: str | None = None) -> dict:
        started = perf_counter()
        chapter = self.store.get_chapter(novel_id, number=number)
        content = chapter["content"] or ""
        if expected_content is not None and expected_content != content:
            raise ReviewConflict("正文尚未保存或已被修改，请保存并刷新后重新审校")
        if not content.strip():
            raise ValueError("请先保存章节正文")
        if len(content) > 100_000:
            raise ValueError("单次审校最多支持 100000 个字符，请拆分章节")
        rules = self.get_rules(novel_id)
        if not rules.profiles or not any(p.allowed_magic or p.forbidden_actions for p in rules.profiles):
            raise ValueError("请先为人物设置至少一项明确限制")
        content_hash, rules_hash = text_hash(content), rules.content_hash()
        cached = self.store.find_run(novel_id, chapter["id"], content_hash, rules_hash)
        if cached:
            return self._decorate(cached, chapter, rules)
        issues, uncertain = review_text(content, rules)
        run = {
            "id": uuid4().hex, "novel_id": novel_id, "chapter_id": chapter["id"],
            "chapter_number": chapter["number"], "content_hash": content_hash,
            "content_snapshot": content, "rules_hash": rules_hash, "engine_version": ENGINE_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": round((perf_counter() - started) * 1000, 3),
            "issues": issues, "uncertain": uncertain,
        }
        saved = self.store.save_run(run)
        return self.get_run(novel_id, saved["id"])

    def get_run(self, novel_id: str, run_id: str) -> dict:
        run = self.store.get_run(novel_id, run_id)
        chapter = self.store.get_chapter(novel_id, chapter_id=run["chapter_id"])
        return self._decorate(run, chapter, self.get_rules(novel_id))

    def latest(self, novel_id: str, number: int, mode: str = "rules") -> dict | None:
        chapter = self.store.get_chapter(novel_id, number=number)
        rules = self.get_rules(novel_id)
        if mode == "ai":
            run = self.store.latest_run(novel_id, chapter["id"], mode="ai")
            return self._decorate(run, chapter, rules) if run else None
        # An author may restore an earlier input. Prefer its reusable result,
        # rather than showing a newer, now-stale run after the next page reload.
        run = self.store.find_run(novel_id, chapter["id"], text_hash(chapter["content"] or ""), rules.content_hash())
        if run is None:
            run = self.store.latest_run(novel_id, chapter["id"])
        return self._decorate(run, chapter, rules) if run else None

    def feedback(self, novel_id: str, run_id: str, issue_id: str, status: str, version: int) -> dict:
        run = self.get_run(novel_id, run_id)
        if run["stale"]:
            raise ReviewConflict("正文或设定已变化，请重新审校后处理问题")
        if not any(i["id"] == issue_id for i in run["issues"]):
            raise ReviewNotFound("未找到该审校问题")
        self.store.feedback(novel_id, run_id, issue_id, status, version)
        return self.get_run(novel_id, run_id)

    def _decorate(self, run: dict, chapter: dict, rules: RuleSet) -> dict:
        reasons = []
        if text_hash(chapter["content"] or "") != run["content_hash"]:
            reasons.append("正文已修改")
        if rules.content_hash() != run["rules_hash"]:
            reasons.append("人物设定已修改")
        ai = run.get("mode") == "ai"
        if (ai and not run["engine_version"].startswith(AI_ENGINE_VERSION + ":")) or (not ai and run["engine_version"] != ENGINE_VERSION):
            reasons.append("审校规则版本已更新")
        if chapter["number"] != run["chapter_number"]:
            reasons.append("章节编号已调整")
        if ai:
            from application.audit.services.history_retrieval import bounded_history, history_digest
            digest = history_digest(bounded_history(self.store.history_rows(run["novel_id"], chapter["number"])))
            if digest != run.get("context_hash"):
                reasons.append("历史正文或章节顺序已变化")
        return {
            **run, "stale": bool(reasons), "stale_reasons": reasons,
            "offset_unit": "unicode_codepoint", "mode": "ai" if ai else "rules",
            "scope": "模型结合给定设定及有限历史片段提出疑似问题；引用已核对，判断仍需作者确认" if ai else "明确人物名、有限元素能力与禁用行为；不覆盖通用情节或时间线推理",
            "model_calls": run.get("model_calls", 0),
        }
